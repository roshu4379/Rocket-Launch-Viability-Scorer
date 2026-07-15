output "public_endpoint" {
  description = "The public URL to test the Phase 2 deployment"
  value       = module.compute.api_endpoint
}