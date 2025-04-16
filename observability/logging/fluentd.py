"""
KUVA AI Fluentd Enterprise Configuration
"""
# fluentd.py - Fluentd v1.15+ Configuration

# ========================
# Global Configuration
# ========================
<system>
  workers 4
  log_level info
  root_dir /var/fluentd
  enable_input_metrics true
  enable_size_metrics true
  <log>
    format json
    time_format "%Y-%m-%dT%H:%M:%S.%N%z"
  </log>
  <rpc>
    bind 0.0.0.0
    port 24444
  </rpc>
</system>

# ========================
# Security Configuration
# ========================
<security>
  self_hostname ${HOSTNAME}
  shared_key "kuva_encrypted_shared_key"
  <client>
    host 0.0.0.0/0
    deny_anonymous false
  </client>
</security>

# ========================
# Input Sources
# ========================
<source>
  @type forward
  @id input_forward
  bind 0.0.0.0
  port 24224
  <transport tls>
    cert_path /etc/fluentd/certs/server.crt
    private_key_path /etc/fluentd/certs/server.key
    client_cert_auth true
    ca_path /etc/fluentd/certs/ca.crt
  </transport>
  <security>
    allow_anonymous_source false
    user_auth true
    <user>
      username kuva_ai
      password "#{ENV['FLUENTD_SECRET_PASSWORD']}"
    </user>
  </security>
</source>

<source>
  @type http
  port 9880
  bind 0.0.0.0
  <parse>
    @type json
    time_type string
    time_format "%Y-%m-%dT%H:%M:%S.%N%z"
  </parse>
</source>

# ========================
# Processing Pipeline
# ========================
<filter **>
  @type parser
  key_name log
  reserve_data true
  <parse>
    @type multi_format
    <pattern>
      format json
      time_key timestamp
      time_type string
      time_format "%Y-%m-%dT%H:%M:%S.%N%z"
    </pattern>
    <pattern>
      format regexp
      expression /^(?<message>.*)$/
    </pattern>
  </parse>
</filter>

<filter **>
  @type record_transformer
  enable_ruby true
  <record>
    hostname ${hostname}
    environment "#{ENV['APP_ENV'] || 'development'}"
    service_name kuva_ai
    log_origin ${tag}
  </record>
</filter>

<filter **>
  @type grep
  <exclude>
    key log_level
    pattern /debug/
  </exclude>
</filter>

# ========================
# Data Sanitization
# ========================
<filter **>
  @type record_modifier
  remove_keys _dummy,_test
  <record>
    message ${record["message"]&.gsub(/(password|token|secret)=[^&\s]+/, '\1=[REDACTED]')}
    client_ip ${record["client_ip"]&.gsub(/(\d+)\.(\d+)\.\d+\.\d+/, '\1.\2.x.x')}
  </record>
</filter>

# ========================
# Buffering Configuration
# ========================
<buffer **>
  @type file
  path /var/fluentd/buffer/${tag}
  flush_mode interval
  flush_interval 5s
  flush_thread_count 8
  retry_type exponential_backoff
  retry_timeout 24h
  chunk_limit_size 32MB
  total_limit_size 64GB
  overflow_action block
  <time>
    timekey 1h
    timekey_wait 10m
  </time>
</buffer>

# ========================
# Output Destinations
# ========================
<match kuva.**>
  @type copy
  <store>
    @type elasticsearch
    host elasticsearch.kuva.ai
    port 9200
    scheme https
    ssl_verify false
    user "#{ENV['ES_USERNAME']}"
    password "#{ENV['ES_PASSWORD']}"
    logstash_format true
    logstash_prefix kuva
    <template>
      enabled true
      name kuva_template
      pattern kuva-*
      template_file /etc/fluentd/templates/elasticsearch.json
    </template>
  </store>

  <store>
    @type s3
    aws_key_id "#{ENV['AWS_ACCESS_KEY_ID']}"
    aws_sec_key "#{ENV['AWS_SECRET_ACCESS_KEY']}"
    s3_bucket kuva-logs
    s3_region us-west-2
    path logs/%Y/%m/%d/
    time_slice_format %H
    store_as gzip
    <instance_profile_credentials>
      ip_address 169.254.169.254
      port 80
    </instance_profile_credentials>
  </store>

  <store>
    @type kafka2
    brokers kafka1.kuva.ai:9092,kafka2.kuva.ai:9092
    default_topic kuva_logs
    sasl_plain_username "#{ENV['KAFKA_USERNAME']}"
    sasl_plain_password "#{ENV['KAFKA_PASSWORD']}"
    ssl_client_cert_key "/etc/fluentd/certs/kafka_client.key"
    ssl_client_cert "/etc/fluentd/certs/kafka_client.crt"
    ssl_ca_cert "/etc/fluentd/certs/kafka_ca.pem"
    <format>
      @type json
    </format>
  </store>
</match>

# ========================
# Monitoring & Alerting
# ========================
<match fluent.**>
  @type prometheus
  <metric>
    name fluentd_output_status
    type counter
    desc Total number of output records
    <labels>
      tag ${tag}
      hostname ${hostname}
    </labels>
  </metric>
</match>

<match fluent.metrics**>
  @type datadog
  api_key "#{ENV['DATADOG_API_KEY']}"
  use_ssl true
  max_retries 5
  http_proxy "#{ENV['HTTP_PROXY']}"
  <buffer>
    @type memory
    flush_interval 10s
  </buffer>
</match>

# ========================
# Fallback Configuration
# ========================
<match **>
  @type file
  path /var/fluentd/fallback/logs
  append true
  <buffer>
    @type file
    path /var/fluentd/buffer/fallback
  </buffer>
  <inject>
    time_key timestamp
    time_type string
  </inject>
</match>

# ========================
# Enterprise Extensions
# ========================
<label @ERROR>
  <match **>
    @type slack
    webhook_url "#{ENV['SLACK_WEBHOOK_URL']}"
    channel alerts
    username fluentd-alerts
    icon_emoji :warning:
    flush_interval 10s
    message "Fluentd Error: %s"
    <buffer tag,time,record>
      @type file
      path /var/fluentd/buffer/slack
    </buffer>
  </match>
</label>

# ========================
# Self-Monitoring
# ========================
<source>
  @type monitor_agent
  bind 0.0.0.0
  port 24220
  tag monitoring
</source>
