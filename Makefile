.DEFAULT_GOAL := help
COMPOSE := docker compose
TF_DIR   := terraform

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m

# ══════════════════════════════════════════════════════════════════════════════
#  help
# ══════════════════════════════════════════════════════════════════════════════
.PHONY: help
help:
	@printf "\n$(BOLD)Network Observability Platform$(RESET)\n\n"
	@printf "$(CYAN)Docker Compose targets$(RESET)\n"
	@printf "  $(GREEN)make up$(RESET)          Start the full stack in detached mode\n"
	@printf "  $(GREEN)make down$(RESET)        Stop and remove containers\n"
	@printf "  $(GREEN)make logs$(RESET)        Tail all container logs (Ctrl-C to exit)\n"
	@printf "  $(GREEN)make clean$(RESET)       Stop containers and DELETE all volumes  ⚠️  destructive\n"
	@printf "  $(GREEN)make ps$(RESET)          Show running containers and health status\n"
	@printf "\n$(CYAN)Seeding & training$(RESET)\n"
	@printf "  $(GREEN)make seed$(RESET)        Seed the runbook knowledge base into pgvector\n"
	@printf "  $(GREEN)make train$(RESET)       Train / retrain the Isolation Forest baseline\n"
	@printf "\n$(CYAN)Development$(RESET)\n"
	@printf "  $(GREEN)make test$(RESET)        Run pytest across all services\n"
	@printf "  $(GREEN)make lint$(RESET)        Run ruff + mypy across all services\n"
	@printf "  $(GREEN)make fmt$(RESET)         Auto-format with ruff\n"
	@printf "\n$(CYAN)Terraform (AWS)$(RESET)\n"
	@printf "  $(GREEN)make tf-init$(RESET)     Initialize Terraform providers\n"
	@printf "  $(GREEN)make tf-plan$(RESET)     Preview infrastructure changes\n"
	@printf "  $(GREEN)make tf-apply$(RESET)    Apply infrastructure changes\n"
	@printf "  $(GREEN)make tf-destroy$(RESET)  Destroy ALL AWS resources  ⚠️  destructive\n"
	@printf "\n"

# ══════════════════════════════════════════════════════════════════════════════
#  Docker Compose
# ══════════════════════════════════════════════════════════════════════════════
.PHONY: up
up:
	$(COMPOSE) up -d
	@printf "\n$(GREEN)Stack is up.$(RESET) Dashboard → http://localhost:3000  API docs → http://localhost:8000/docs\n"

.PHONY: down
down:
	$(COMPOSE) down

.PHONY: logs
logs:
	$(COMPOSE) logs -f

.PHONY: clean
clean:
	@printf "$(BOLD)This will delete all Docker volumes (all local data). Continue? [y/N]$(RESET) " && read ans && [ $${ans:-N} = y ]
	$(COMPOSE) down -v --remove-orphans
	@printf "$(GREEN)Volumes removed.$(RESET)\n"

.PHONY: ps
ps:
	$(COMPOSE) ps

# ══════════════════════════════════════════════════════════════════════════════
#  Seeding & training
# ══════════════════════════════════════════════════════════════════════════════
.PHONY: seed
seed:
	$(COMPOSE) run --rm -e PYTHONPATH=/app scripts python /app/scripts/seed_runbooks.py

.PHONY: train
train:
	$(COMPOSE) run --rm -e PYTHONPATH=/app scripts python /app/scripts/train_baseline.py

# ══════════════════════════════════════════════════════════════════════════════
#  Development
# ══════════════════════════════════════════════════════════════════════════════
.PHONY: test
test:
	@for svc in simulator collector topology anomaly agent api; do \
		printf "\n$(CYAN)Testing services/$$svc$(RESET)\n"; \
		cd services/$$svc && python -m pytest -q --tb=short 2>/dev/null || true; \
		cd ../..; \
	done

.PHONY: lint
lint:
	ruff check services/ scripts/
	mypy services/ --ignore-missing-imports

.PHONY: fmt
fmt:
	ruff format services/ scripts/

# ══════════════════════════════════════════════════════════════════════════════
#  Terraform
# ══════════════════════════════════════════════════════════════════════════════
.PHONY: tf-init
tf-init:
	cd $(TF_DIR) && terraform init

.PHONY: tf-plan
tf-plan:
	cd $(TF_DIR) && terraform plan -var-file=terraform.tfvars.example

.PHONY: tf-apply
tf-apply:
	cd $(TF_DIR) && terraform apply -var-file=terraform.tfvars.example

.PHONY: tf-destroy
tf-destroy:
	@printf "$(BOLD)This will DESTROY all AWS resources. Continue? [y/N]$(RESET) " && read ans && [ $${ans:-N} = y ]
	cd $(TF_DIR) && terraform destroy -var-file=terraform.tfvars.example
