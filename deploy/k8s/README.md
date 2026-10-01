# Kubernetes manifests (GKE)

The full guide is **[docs/deployment/gcp-gke.md](../../docs/deployment/gcp-gke.md)** (infrastructure, CI/CD,
operations). This file is a quick map.

```
base/                  every workload, config, SecretProviderClasses, NetworkPolicies, HPAs/PDBs,
                       Ingress + managed cert + Cloud Armor hook, PodMonitoring, job templates
components/
  staging-data/        db-seed job + workflow smoke test (staging only; never production)
  litellm/             optional LiteLLM backend (LLM_BACKEND=litellm)
overlays/
  staging/             config.env (runtime config), values.env (Terraform outputs), scaling patches
  production/          config.env (OIDC/JWKS), values.env, replica floors, IdP egress policy
render.sh              kubectl kustomize + envsubst (explicit allowlist) -> stdout
deploy.sh              render -> dry run -> pre-phase -> migrate (+seed) -> apply -> rollout -> smoke
                       -> ingest; rolls every Deployment back if rollout or smoke fails
run-job.sh             start a Job from a suspended CronJob template (optionally overriding the command)
```

Common commands:

```bash
deploy/k8s/render.sh staging "$IMAGE" | less                    # inspect exactly what would be applied
```

```bash
make k8s-validate                                               # render both overlays + kubeconform (Docker)
```

```bash
deploy/k8s/deploy.sh staging "$IMAGE"                           # what CI runs (needs cluster credentials)
```

```bash
deploy/k8s/run-job.sh ops-toolbox -- python -m ordering.prompts.rollouts list
```

The manifests reference infrastructure only through `${…}` placeholders filled from
`overlays/<env>/values.env`: project, region, image, hostname, static IP, Cloud Armor and SSL policy
names, private services CIDR, and buckets. Secrets never appear in this directory.
