data "kubernetes_namespace" "storage_api" {
  metadata {
    name = var.namespace
  }
}

data "terraform_remote_state" "cluster_config" {
  backend = "local"

  config = {
    path = "${path.module}/../../cluster-config/tofu/terraform.tfstate"
  }
}

locals {
  instance_name = "storage-api"

  registry_host = split("/", var.image_repository)[0]
  registry_repo = join("/", slice(split("/", var.image_repository), 1, length(split("/", var.image_repository))))
}

data "external" "storage_api_latest_tag" {
  program = ["python3", "${path.module}/scripts/latest_image_tag.py"]

  query = {
    registry_host = local.registry_host
    repo          = local.registry_repo
    prefix        = var.image_tag_prefix
    username      = data.terraform_remote_state.cluster_config.outputs.clusterkeep_ui_registry_username
    password      = data.terraform_remote_state.cluster_config.outputs.clusterkeep_ui_registry_password
  }
}

data "external" "storage_api_release_tag" {
  count   = var.image_tag_prefix == "" && var.image_tag == "" ? 1 : 0
  program = ["python3", "${path.module}/scripts/latest_release_tag.py"]

  query = {
    repo_dir = abspath("${path.module}/..")
  }
}

locals {
  deployed_image_tag = (
    var.image_tag_prefix != ""
    ? (data.external.storage_api_latest_tag.result.tag != "" ? data.external.storage_api_latest_tag.result.tag : var.image_tag)
    : (var.image_tag != "" ? var.image_tag : data.external.storage_api_release_tag[0].result.tag)
  )
}

resource "random_password" "key_pepper" {
  length  = 64
  special = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "random_password" "key_cache_secret" {
  length  = 64
  special = false
}

resource "kubernetes_secret" "storage_api" {
  metadata {
    name      = "${local.instance_name}-secrets"
    namespace = data.kubernetes_namespace.storage_api.metadata[0].name
  }

  data = {
    STORAGE_API_DATABASE_URL = format(
      "postgresql+asyncpg://storage_api:%s@%s.%s.svc.cluster.local:5432/storage_api",
      random_password.postgres.result,
      kubernetes_service.postgres.metadata[0].name,
      data.kubernetes_namespace.storage_api.metadata[0].name,
    )
    STORAGE_API_SERVICE_TOKEN    = var.service_token
    STORAGE_API_KEY_PEPPER       = random_password.key_pepper.result
    STORAGE_API_KEY_CACHE_SECRET = random_password.key_cache_secret.result
  }
}

resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name      = "${local.instance_name}-data"
    namespace = data.kubernetes_namespace.storage_api.metadata[0].name
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = "nfs-csi"

    resources {
      requests = {
        storage = var.data_storage_size
      }
    }
  }

  wait_until_bound = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "helm_release" "storage_api" {
  name      = local.instance_name
  chart     = "${path.module}/../../charts/storage-api"
  namespace = data.kubernetes_namespace.storage_api.metadata[0].name

  atomic = true

  values = [
    yamlencode({
      replicaCount = var.replica_count
      image = {
        repository = var.image_repository
        tag        = local.deployed_image_tag
      }
      config = {
        environment        = var.environment
        redisNamespace     = "storageapi"
        authApiInternalUrl = var.auth_api_internal_url
        dataDir            = "/data"
        defaultQuotaBytes  = tostring(var.default_quota_bytes)
      }
      redis = {
        host     = kubernetes_service.redis.metadata[0].name
        port     = 6379
        database = 0
      }
      existingSecret = kubernetes_secret.storage_api.metadata[0].name
      dataClaimName  = kubernetes_persistent_volume_claim_v1.data.metadata[0].name
      imagePullSecrets = var.image_pull_secret_name != "" ? [
        { name = var.image_pull_secret_name }
      ] : []
    })
  ]

  depends_on = [
    kubernetes_stateful_set_v1.postgres,
    kubernetes_deployment_v1.redis,
  ]
}
