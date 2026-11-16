output "internal_url" {
  description = "In-cluster address storage-ui and clusterkeep-ui call."
  value       = "http://${local.instance_name}.${data.kubernetes_namespace.storage_api.metadata[0].name}.svc.cluster.local"
}
