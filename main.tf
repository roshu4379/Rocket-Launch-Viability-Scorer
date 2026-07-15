terraform {
  required_version = ">= 1.1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "rocket-launch-viability-scorer-tfstate-bucket"
    key          = "capstone/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}

data "aws_iam_role" "lab" {
  name = "LabRole"
}

resource "aws_s3_bucket" "state" {
  bucket = "rocket-launch-viability-scorer-tfstate-bucket"
}

module "security" {
  source  = "./modules/security"
  project = var.project
}

module "compute" {
  source       = "./modules/compute"
  project      = var.project
  lab_role_arn = data.aws_iam_role.lab.arn
  secret_name  = module.security.secret_name
}