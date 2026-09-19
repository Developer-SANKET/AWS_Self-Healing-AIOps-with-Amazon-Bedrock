# AWS Self-Healing Infrastructure with Amazon Bedrock

> An event-driven DevOps/SRE project that detects application incidents, collects diagnostics, uses Amazon Bedrock for incident reasoning, applies a safety gate, and performs controlled remediation or escalation — across EC2, EKS, and ECS.

![AWS](https://img.shields.io/badge/AWS-Cloud-orange)
![Bedrock](https://img.shields.io/badge/Amazon%20Bedrock-AI%20Reasoning-8250df)
![Python](https://img.shields.io/badge/Python-3.x-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-Application-009688)
![AIOps](https://img.shields.io/badge/AIOps-Self--Healing-purple)
![EKS](https://img.shields.io/badge/EKS-Kubernetes-326CE5)
![ECS](https://img.shields.io/badge/ECS-Containers-FF9900)

## Project in One Line

**Detect → Diagnose → Reason → Safely Remediate → Verify → Record / Escalate**

The important design choice is that the **AIOps control plane stays the same**, while the remediation method changes depending on where the application is running:

| Runtime | Application | Remediation interface | Scaling / availability |
|---|---|---|---|
| **EC2** | FastAPI on EC2 | AWS Systems Manager (SSM) | EC2 Auto Scaling + ALB |
| **EKS** | FastAPI in Pods | Kubernetes API / `kubectl` / approved controllers | HPA + Kubernetes self-healing + ALB |
| **ECS** | FastAPI in containers | ECS API / service deployment | ECS Service Auto Scaling + ALB |

**Current portfolio focus:** EC2 baseline + EKS extension.
**ECS:** optional third runtime using the same AIOps pattern.

**Why split it this way?** A reviewer's first question for any "self-healing" project is *"self-healing at which layer, and who is actually allowed to make changes?"* Separating a fixed **AIOps brain** from swappable **runtime adapters** answers that up front: the reasoning and the safety rules never change, only the API used to act does.

---

## Main Architecture

![Main architecture diagram — user traffic through ALB, EC2/EKS/ECS runtime, CloudWatch, EventBridge, Lambda AIOps controller, Bedrock reasoning, Safety Gate, then approved remediation (green) or escalation (red) into DynamoDB](docs/architecture/main-architecture.png)

### What this means, and why each stage exists

- **Application Runtime** runs the workload.
  *Why:* this is the thing actually being protected. It's drawn as three interchangeable icons (EC2 / EKS / ECS) to make the point that the AIOps logic doesn't care which one is underneath.
- **CloudWatch** collects metrics/logs and detects abnormal conditions.
  *Why:* you can't reason about an incident you haven't observed. CloudWatch is the single ingestion point so every runtime reports incidents in the same shape.
- **EventBridge** receives the incident event.
  *Why:* decouples "something happened" from "something responds." Lambda isn't polling anything — it's invoked only when there's real signal.
- **Lambda** becomes the central AIOps controller.
  *Why:* serverless compute means no idle cost and no server of its own to fail. It's also the one auditable chokepoint that orchestrates evidence-gathering, calls Bedrock, and enforces the safety gate.
- Lambda gathers diagnostics from the correct runtime.
  *Why:* an LLM reasoning over a bare alarm name ("CPUUtilization > 80%") will guess. Reasoning over real evidence produces a defensible root cause instead of a hallucinated one.
- **Amazon Bedrock** analyzes the incident context and recommends an action.
  *Why:* this is the "AIOps" part — using a model to correlate signals a human would otherwise have to triage manually at 3am, without hosting or patching a model yourself.
- **Safety Gate** decides whether that action is allowed automatically.
  *Why:* this is the most important box in the whole diagram. A Bedrock recommendation is a *suggestion*, not a command — the gate is what stops a hallucinated or low-confidence action from ever touching production.
- The system either **remediates** or **escalates**.
  *Why:* every incident needs one of exactly two deterministic outcomes — never a silent, unapproved third option.
- A **health check** verifies whether recovery worked.
  *Why:* remediation isn't assumed to work just because it ran. A still-unhealthy app after the approved action is itself a new signal, not a success to log.
- **DynamoDB** stores the incident and outcome.
  *Why:* auditability. Every decision — detected, reasoned, approved/denied, remediated, verified — is reconstructable after the fact, which is what an SRE post-incident review needs.

---

## EC2 Runtime

![EC2 runtime diagram — internet user through ALB and Auto Scaling Group to FastAPI on EC2, CloudWatch, EventBridge, Lambda, SSM diagnostics, Bedrock, Safety Gate, then SSM restart or SNS escalation into DynamoDB](docs/architecture/runtime-ec2.png)

An ALB health-check failure or an abnormal CloudWatch metric triggers EventBridge, which invokes the Lambda controller. Lambda pulls diagnostics through SSM, sends that evidence to Bedrock, and the Safety Gate decides between an SSM restart (green path) or an SNS escalation (red path), with the outcome verified and logged to DynamoDB either way.

**Why SSM and not SSH?** Systems Manager Run Command lets Lambda execute an approved, logged command against a specific instance without opening SSH, managing keys, or granting broad EC2 control-plane permissions. The IAM policy can be scoped to one document and one tag — a much smaller blast radius than "Lambda can call `ec2:*`."

### Why ALB + Auto Scaling are separate from AIOps

**ALB + Auto Scaling** handle normal application availability and capacity management.
**AIOps** handles diagnosis and controlled remediation.

```text
Traffic increases → ALB receives more requests → ASG scaling policy adds EC2 capacity
```

This is normal infrastructure scaling; AIOps does not need to intervene.

```text
FastAPI fails → Monitoring detects incident → AIOps investigates → AIOps remediates or escalates
```

**Why keep these separate?** It's tempting to route every alarm through the AI layer, but that means paying LLM-reasoning latency and cost for problems AWS-native automation already solves deterministically. Reserving AIOps for genuine diagnosis work is what makes this an engineering decision rather than "AI for everything."

AWS Auto Scaling target tracking can scale an Auto Scaling group from metrics such as average CPU utilization or ALB request count per target, while ELB health checks can be used by Auto Scaling when determining instance health.
References: [EC2 Auto Scaling target tracking](https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-scaling-target-tracking.html), [Auto Scaling health checks](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-health-checks.html)

---

## EKS Runtime

The **AIOps flow does not change**. Only the runtime-specific diagnosis and remediation change.

![EKS runtime diagram — user through ALB, Ingress, Service to FastAPI Pods managed by HPA, CloudWatch, EventBridge, Lambda, Kubernetes API, Bedrock, Safety Gate, then approved K8s action or SNS escalation into DynamoDB](docs/architecture/runtime-eks.png)

| EC2 | EKS |
|---|---|
| FastAPI runs as a Linux service/process on EC2 | FastAPI runs inside Pods |
| SSM executes shell commands | Kubernetes API performs approved workload operations |
| ASG scales EC2 instances | HPA scales Pods |
| ALB Target Group points to EC2 targets | AWS Load Balancer Controller can provision/manage ALBs from Kubernetes resources |
| Instance replacement is an EC2/ASG capability | Pod replacement is a Kubernetes capability |

**Why swap the adapter instead of rewriting the controller?** The Lambda controller, the Bedrock prompt shape, the safety-gate rules, and the DynamoDB schema are all runtime-agnostic. Only "collect evidence" and "take action" change their implementation (SSM calls → Kubernetes API calls) — that's the Layer 1 / Layer 2 split below in action.

The AWS Load Balancer Controller manages AWS load balancers from Kubernetes `Ingress` and `Service` resources. An `Ingress` can create an ALB, while a `Service` of type `LoadBalancer` can create an NLB.
Reference: [AWS Load Balancer Controller for EKS](https://docs.aws.amazon.com/eks/latest/userguide/aws-load-balancer-controller.html)

Kubernetes HPA automatically adjusts the number of Pods based on metrics such as CPU utilization and requires a metrics source such as the Kubernetes Metrics Server.
Reference: [EKS Horizontal Pod Autoscaler](https://docs.aws.amazon.com/eks/latest/userguide/horizontal-pod-autoscaler.html)

### EKS Incident Examples

**Pod crash (native self-healing):**

```text
FastAPI Pod crashes → Kubernetes detects failed Pod → ReplicaSet/Deployment creates a replacement → Service routes to healthy Pods
```

This is native Kubernetes self-healing. AIOps does not need to restart a Pod merely because it crashed.
**Why call this out explicitly?** It pre-empts the obvious interview question — "doesn't Kubernetes already self-heal, so what does your AI layer even do?" AIOps stays out of the way until the platform's own automation has clearly failed to resolve the problem — for example, when the Pod enters CrashLoopBackOff or stays unhealthy despite restarting, which is the scenario the diagram above covers. A single crash is noise; a repeated pattern is the signal that justifies AI involvement.

**High CPU (native scaling vs. AIOps):**

```text
High CPU → HPA detects metric → More Pods → Traffic distributed across Pods
```

That's normal, native scaling. AIOps becomes useful when high CPU is accompanied by other evidence — elevated error rate, unusual logs — because CPU alone can't tell you if the app is scaling healthily under load or churning CPU on a bug (e.g. an infinite retry loop). Correlating CPU with errors is what turns a scaling event into a diagnosable incident.

---

## ECS Runtime (Optional Extension)

ECS is useful as a third scenario because the same container image can be deployed as an ECS task instead of a Kubernetes Pod.

![ECS runtime diagram — user through ALB to FastAPI Tasks deployed from Amazon ECR, CloudWatch, EventBridge, Lambda, Bedrock, Safety Gate, then ECS API remediation or SNS escalation into DynamoDB](docs/architecture/runtime-ecs.png)

The control plane is identical to the one below in **The Common AIOps Control Plane** — only the final action changes: `restart / redeploy ECS service`, `force a new deployment`, or `increase desired task count` (when explicitly allowed).

**Why include a third runtime at all?** One adapter could be a coincidence; two adapters (EC2 + EKS) look like a deliberate pattern. A third (ECS) is the strongest evidence that the brain/adapter split is real architecture, not just a diagram.

ECS Service Auto Scaling uses Application Auto Scaling and CloudWatch metrics to change the desired task count based on target tracking, step scaling, or scheduled scaling.
Reference: [ECS Service Auto Scaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html)

Container images are stored in Amazon ECR and referenced by ECS task definitions. **ECS is optional in this project** — a useful extension once EC2 and EKS are working cleanly.

---

## The Common AIOps Control Plane

This is the most important part of the project.

![The common AIOps control plane — Detect, Route Event, Controller, Collect Evidence, AI Reasoning, Safety Gate, then Remediate/Verify/Store (green) or Escalate/Store (red)](docs/architecture/aiops-control-plane.png)

**Detect** — Monitoring identifies an abnormal condition (application health failure, high error rate, abnormal CPU/memory, repeated container failures, repeated Kubernetes restarts).
*Why:* every downstream decision inherits the quality of this first signal — if detection is noisy, everything after it inherits that noise.

**Collect Evidence** — Lambda gathers context before deciding: CloudWatch metrics, application logs, EC2 status, SSM diagnostics, Kubernetes Pod status/logs, ECS task/service status.
*Why:* this is what separates "AIOps" from "a chatbot glued to an alarm." The model only ever reasons over real, structured evidence — never a bare metric name.

**AI Reasoning** — Bedrock receives structured incident context and returns a structured analysis:

```json
{
  "severity": "HIGH",
  "root_cause": "Application health check failure",
  "recommended_action": "restart_service",
  "confidence": 0.91
}
```

The recommendation is **not executed directly**.
*Why structured JSON, not free text:* the Safety Gate needs to evaluate a `confidence` number and a `recommended_action` enum deterministically — it can't parse prose reliably.

**Safety Gate** — checks the recommendation against predefined rules: is the action allowed, is the target resource approved, is confidence above threshold, is the action reversible, is it inside the expected scope.
*Why deterministic, not another AI call:* this is the control that keeps a probabilistic model out of the production blast radius. It's ordinary allow-listing logic on purpose — boring, testable, auditable — because the one decision you never want "creative" is the one that actually changes infrastructure.

**Remediation** — the runtime adapter performs the approved action (EC2 → SSM, EKS → Kubernetes API, ECS → ECS API).
*Why per-runtime adapters:* each platform has its own least-privilege action surface. Keeping them separate means the IAM policy for "restart an EC2 service" never accidentally grants "delete a Kubernetes Deployment."

**Verification** — a health check confirms whether the incident actually recovered; unhealthy routes to escalation instead.
*Why:* "we ran the fix" and "the fix worked" are different claims. Verifying closes the loop and prevents a false "resolved" from being recorded.

**Incident Record** — DynamoDB stores the event, diagnosis, action, outcome, and timestamps.
*Why:* this is the artifact a reviewer, teammate, or future you actually reads. Every automated action needs a paper trail, especially one an AI model influenced.

---

## Why the Same Design Works Across EC2, EKS and ECS

Think of the project as two layers.

**Layer 1 — Common AIOps Brain:** CloudWatch, EventBridge, Lambda, Bedrock, Safety Gate, SNS, DynamoDB.

**Layer 2 — Runtime Adapter:**

```text
               AIOps Controller
                     │
       ┌─────────────┼─────────────┐
       ↓             ↓             ↓
     EC2            EKS           ECS
       ↓             ↓             ↓
     SSM        Kubernetes API   ECS API
       ↓             ↓             ↓
    FastAPI        Pods         Tasks
```

This makes the project extensible without creating three completely different AIOps systems.

**Why this split is the headline of the project:** it's a direct answer to "how would this scale to a fourth runtime (say, bare Lambda functions or on-prem)?" — the answer is "add one more adapter under Layer 2; Layer 1 doesn't change." That's a much stronger answer than describing three separate pipelines.

---

## Recommended Project Structure

```text
aws-self-healing-aiops/
│
├── app/
│   └── fastapi/
│       ├── main.py
│       └── requirements.txt
│
├── lambda/
│   └── incident-handler/
│       └── lambda_function.py
│
├── ec2/
│   ├── user-data.sh
│   └── service/
│
├── eks/
│   ├── namespace.yaml
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── hpa.yaml
│   └── configmap.yaml
│
├── ecs/
│   ├── task-definition.json
│   └── service-notes.md
│
├── monitoring/
│   ├── cloudwatch-agent.json
│   ├── alarms.md
│   └── dashboards.md
│
├── aiops/
│   ├── prompts/
│   └── safety-rules.md
│
├── terraform/
│   └── infrastructure/
│
├── docs/
│   └── architecture/
│       ├── main-architecture.png
│       ├── aiops-control-plane.png
│       ├── runtime-ec2.png
│       ├── runtime-eks.png
│       └── runtime-ecs.png
│
└── README.md
```

---

## Build Roadmap

**Phase 1 — EC2 Baseline ✅**
`EC2 → FastAPI → CloudWatch → EventBridge → Lambda → SSM + Bedrock → Safety Gate → Remediate / SNS → Health Check → DynamoDB`

**Phase 2 — EC2 Production Layer 🔜**
`ALB → Target Group → Auto Scaling Group → Multiple EC2 instances → FastAPI`
Add: ALB health checks, Auto Scaling target tracking, min/desired/max capacity, multi-AZ placement, clean deployment bootstrap.

**Phase 3 — EKS 🚀**
`EKS Cluster → Deployment → FastAPI Pods → Service → Ingress / ALB → HPA`
Then add EKS-specific AIOps diagnostics and approved remediation.

**Phase 4 — ECS (Optional) 🐳**
`Docker image → ECR → ECS Service → Tasks → ALB → ECS Service Auto Scaling`
Then connect the same AIOps control plane to ECS.

**Why phase it this way?** Each phase is independently demo-able and defensible — EC2 baseline proves the control plane works at all; the production layer proves it survives real availability concerns; EKS proves the adapter pattern generalizes; ECS (optional) proves it generalizes twice. Easier to narrate than "I built everything at once."

---

## Suggested Failure Scenarios

| Scenario | Native platform response | AIOps role |
|---|---|---|
| EC2 CPU increases | ASG can scale out | Optional diagnosis/correlation |
| EC2 instance becomes unhealthy | ASG can replace instance | Investigate repeated failures |
| FastAPI process stops on EC2 | ALB health check fails | Diagnose + approved SSM restart |
| EKS Pod crashes | Kubernetes replaces Pod | Investigate repeated crashes |
| EKS high CPU | HPA can scale Pods | Correlate with errors/logs |
| EKS application returns 5xx | Service may remain technically healthy | Diagnose application problem |
| ECS task stops | ECS service maintains desired tasks | Investigate repeated task failures |
| ECS service under load | ECS Service Auto Scaling scales tasks | Analyze abnormal behavior |

The project becomes more credible when AIOps is used for problems that **native platform automation does not already solve well**.

---

## Security and Safety Principles

The AI layer should never receive unrestricted infrastructure control. A Bedrock recommendation only ever reaches an approved AWS/Kubernetes API call after passing through the allow-listed Safety Gate.

**Safer automatic actions:**

```text
EC2:  restart approved application service
EKS:  restart approved Deployment/Pod
      scale approved Deployment within limits
ECS:  force approved service deployment
      change desired task count within limits
```

**Actions that should normally require escalation:**

```text
delete infrastructure
modify IAM permissions
change VPC networking
change security groups broadly
terminate arbitrary production instances
execute arbitrary AI-generated shell commands
```

**Why this list matters more than the diagrams:** anyone can draw a box labeled "Safety Gate." What makes it real is naming, concretely, the actions that are never eligible for automatic execution regardless of model confidence. That's the difference between "AI-assisted remediation" and "an AI with root access."

---

## Observability

`Application → FastAPI /health → Logs + Metrics → CloudWatch → Incident Detection`

For EKS, add: Pod metrics, Pod logs, Deployment status, HPA status, Node status, Kubernetes events.
For ECS, add: Task health, Service desired/running count, Container logs, CPU/memory utilization, ALB target health.

**Why runtime-specific observability lists:** the "Collect Evidence" step is only as good as what's actually available to collect. This is a reminder that the evidence Bedrock reasons over has to be built before the AI layer can be useful.

---

## Example Incident Record

```json
{
  "incident_id": "INC-00123",
  "runtime": "EKS",
  "resource": "fastapi-deployment",
  "trigger": "application_health_failure",
  "severity": "HIGH",
  "root_cause": "Repeated application container failures",
  "recommended_action": "restart_deployment",
  "confidence": 0.91,
  "safety_decision": "APPROVED",
  "remediation_status": "SUCCESS",
  "health_check": "HEALTHY"
}
```

This creates an auditable trail from detection to recovery.

---

## What Makes This a DevOps / SRE Project

This project demonstrates several real-world concepts together: AWS infrastructure, Linux and application troubleshooting, load balancing, Auto Scaling, containers, Kubernetes, infrastructure automation, CI/CD compatibility, monitoring and observability, event-driven automation, incident response, safe automated remediation, AI-assisted root-cause analysis, and auditability.

The key idea is **not "AI replaces DevOps."**

> **Use AI to improve incident diagnosis and decision-making, while deterministic automation and safety rules control what infrastructure actions are actually allowed.**

---

## Interview Architecture Summary

> "I built an event-driven self-healing platform on AWS. The application runs on an infrastructure runtime such as EC2, EKS, or ECS. CloudWatch detects abnormal behavior and EventBridge triggers a Lambda-based incident controller. The controller collects runtime diagnostics, sends the incident context to Amazon Bedrock for structured reasoning, and passes the recommendation through a safety gate. Based on the approved action, it uses SSM for EC2, the Kubernetes API for EKS, or the ECS API for containers. After remediation it performs a health check, records the incident in DynamoDB, and sends an SNS notification when the system cannot safely recover."

---

## References

- [Amazon EC2 Auto Scaling — Target Tracking](https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-scaling-target-tracking.html)
- [Amazon EC2 Auto Scaling — Health Checks](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-health-checks.html)
- [Amazon EKS — AWS Load Balancer Controller](https://docs.aws.amazon.com/eks/latest/userguide/aws-load-balancer-controller.html)
- [Amazon EKS — Horizontal Pod Autoscaler](https://docs.aws.amazon.com/eks/latest/userguide/horizontal-pod-autoscaler.html)
- [Amazon ECS — Service Auto Scaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html)

## Project Status

| Component | Status |
|---|---|
| EC2 FastAPI application | ✅ Implemented |
| CloudWatch monitoring | ✅ Implemented |
| EventBridge | ✅ Implemented |
| Lambda incident handler | ✅ Implemented |
| SSM remediation | ✅ Implemented |
| Bedrock reasoning | ✅ Implemented |
| Safety Gate | ✅ Implemented / refining |
| SNS escalation | ✅ Implemented |
| DynamoDB incident history | ✅ Implemented |
| ALB + EC2 Auto Scaling | 🔜 Next |
| EKS deployment | 🔜 Next |
| EKS AIOps remediation | 🔜 Next |
| ECS runtime adapter | 🟡 Optional |
