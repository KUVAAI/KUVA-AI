# data_retention.hcl

# ========================
# Global Retention Settings
# ========================
globals {
  default_retention_unit = "years"
  legal_hold_grace_days  = 30
  encryption_standard    = "AES-256-GCM"
  key_management_service = "vault://kuva-secrets/kms/prod"
}

# ======================
# Data Classification
# ======================
data_classification "gdpr_personal_data" {
  identifiers = ["email", "phone", "ip_address"]
  regex_patterns = [
    "/\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}\\b/",
    "/\\+?\\d{1,4}[-.\\s]?\\(?\\d{1,4}\\)?[-.\\s]?\\d{1,4}[-.\\s]?\\d{1,9}/"
  ]
  compliance_references = ["GDPR Art.17", "CCPA §1798.105"]
}

data_classification "hipaa_phi" {
  identifiers = ["patient_id", "diagnosis_code", "treatment_date"]
  compliance_references = ["HIPAA 164.312", "HITECH §13402"]
}

# ======================
# Retention Rules
# ======================
retention_rule "agent_training_data" {
  data_types      = ["dataset", "model_artifacts"]
  storage_locations = [
    "s3://kuva-ai-data/region=*",
    "gcs://kuva-ml-backups/",
  ]
  
  retention_period {
    active_use     = "3 years"
    archive        = "7 years"
    legal_hold     = "indefinite"
  }

  auto_delete {
    enabled         = true
    trigger_conditions = [
      "retention_period.expired",
      "user.delete_request_verified"
    ]
    approval_workflow {
      required_roles = ["compliance-officer", "data-steward"]
      max_approval_duration = "72h"
    }
  }
}

retention_rule "audit_logs" {
  data_types      = ["access_logs", "policy_decisions"]
  storage_locations = [
    "splunk://kuva-audit",
    "s3://kuva-archive/logs/year=*",
  ]

  retention_period {
    active_monitoring = "1 year"
    warm_storage      = "5 years"
    cold_storage      = "10 years"
  }

  immutable_storage {
    enabled          = true
    object_lock_mode = "GOVERNANCE"
    legal_hold_controls = {
      litigation_hold   = "vault://legal/litigation-holds"
      preservation_order = "vault://legal/preservation-orders"
    }
  }
}

# ======================
# Backup & Archival
# ======================
backup_policy "critical_ai_models" {
  scope           = "tag:critical=true"
  frequency       = "0 0 * * 0" # Weekly Sundays
  retention       = "10y"
  storage_class   = "GLACIER_DEEP_ARCHIVE"
  
  encryption {
    algorithm      = globals.encryption_standard
    key_rotation   = "90d"
    dual_control   = true
  }

  validation {
    checksum_verify  = true
    test_restore     = "quarterly"
    integrity_checks = "biannual"
  }
}

# ======================
# Access & Audit
# ======================
audit_config "data_access_monitoring" {
  log_destinations = [
    "splunk://kuva-audit-prod",
    "kafka://kuva-siem-ingest"
  ]
  
  alert_rules {
    anomalous_access = {
      threshold     = "5 failed attempts in 10m"
      severity      = "P1"
      response_plan = "playbooks/data-breach-response"
    }
    
    sla_violation = {
      delete_attempts_after_retention = "3"
      escalation_path = ["compliance@kuva.ai", "security-ops@kuva.ai"]
    }
  }
}

# ======================
# Deletion Workflows
# ======================
deletion_workflow "gdpr_right_to_be_forgotten" {
  trigger_conditions = [
    "user.opt_out_validated",
    "legal.order_received"
  ]
  
  phases = [
    {
      name          = "Data Identification"
      timeout       = "24h"
      tools         = ["bigquery_scanner", "s3_inventory"]
    },
    {
      name          = "Secure Erasure"
      method        = "crypto_shred"
      verification  = "post_shred_checksum"
    },
    {
      name          = "Audit Trail"
      documentation = "vault://legal/erasure-reports/{{request_id}}"
    }
  ]
}

# ======================
# Compliance Reporting
# ======================
compliance_report "quarterly_retention_review" {
  schedule        = "0 0 1 */3 *" # Quarterly on 1st
  recipients      = ["compliance-team@kuva.ai", "dpo@kuva.ai"]
  data_sources    = [
    "s3://kuva-ai-data",
    "splunk://kuva-audit",
    "vault://legal/holds"
  ]
  
  metrics = [
    "retention_policy_compliance_rate",
    "average_deletion_latency",
    "legal_hold_volume"
  ]
}

# ======================
# Test & Validation
# ======================
test "retention_policy_validation" {
  frequency       = "0 0 1 * *" # Monthly
  scenarios       = [
    "test_data_expiry_workflow",
    "test_legal_hold_isolation",
    "test_crypto_shredding"
  ]
  
  failure_actions = [
    "freeze_deletion_workflows",
    "alert=data-retention-failures"
  ]
}
