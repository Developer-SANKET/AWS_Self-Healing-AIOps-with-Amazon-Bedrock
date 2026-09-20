
# 🤖 AWS Self-Healing Infrastructure with Amazon Bedrock

> **Detect → Diagnose → Reason → Safety Check → Remediate / Escalate → Verify → Record**

An event-driven AIOps project that detects application incidents, collects runtime diagnostics, uses **Amazon Bedrock** for incident reasoning, applies a deterministic **Safety Gate**, performs controlled remediation, verifies recovery, and stores the incident outcome.

## 🏗️ Project Architecture

The project uses **one AIOps control plane** with different remediation methods for different runtimes.

```text
                         ☁️ AWS / AIOps CONTROL PLANE

      ┌───────────────┐
      │  Detection    │
      │ CloudWatch /  │
      │ Prometheus    │
      │ Grafana       │
      └───────┬───────┘
              │
              ▼
        ⚡ EventBridge
              │
              ▼
        λ AWS Lambda
       AIOps Controller
              │
        ┌─────┴─────┐
        ▼           ▼
   🔎 Runtime    🧠 Amazon
   Diagnostics     Bedrock
        │           │
        └─────┬─────┘
              ▼
        🛡️ Safety Gate
          /         \
         /           \
        ▼             ▼
   🔧 Remediate    🚨 Escalate
        │             │
        ▼             ▼
   EC2 / EKS / ECS   📧 SNS
        │
        ▼
   ❤️ Health Check
        │
        ▼
   🗄️ DynamoDB
```

### Runtime actions

```text
λ AIOps Controller
       │
       ├──────────────► 🖥️ EC2
       │                  │
       │                  └── 🔐 SSM
       │                       └── restart approved service
       │
       ├──────────────► ☸️ EKS
       │                  │
       │                  └── Kubernetes API
       │                       └── approved workload action
       │
       └──────────────► 📦 ECS (optional)
                          │
                          └── ECS API
                               └── approved service action
```

**The AIOps flow stays the same. The runtime remediation interface changes.**

---

# 🖥️ EC2 Flow — Current Baseline

## Normal application flow

```text
👤 User
   │
   ▼
🌐 Application Load Balancer
   │
   ▼
🎯 Target Group
   │
   ▼
🖥️ EC2 + FastAPI :8000
   │
   ▼
✅ Application response
```

### Capacity handling

```text
More traffic
    ↓
ALB request / EC2 metrics
    ↓
EC2 Auto Scaling Group
    ↓
Additional EC2 instances
    ↓
ALB distributes traffic
```

**ALB + Auto Scaling handle normal availability and capacity.**

## 🚨 EC2 incident flow

```text
                    👤 USER
                       │
                       ▼
                  🌐 AWS ALB
                       │
                       ▼
              🖥️ EC2 + FastAPI
                       │
                  ❌ App failure
                       │
                       ▼
                ☁️ CloudWatch
                       │
                  Alarm = ALARM
                       │
                       ▼
                ⚡ EventBridge
                       │
                       ▼
                 λ Lambda
              AIOps Controller
                 /         \
                /           \
        🔎 SSM Diagnostics  🧠 Bedrock
                \           /
                 \         /
                  ▼       ▼
                  🛡️ Safety Gate
                   /       \
                  /         \
                 ▼           ▼
          🔧 Remediate     🚨 Escalate
                 │             │
                 ▼             ▼
             🔐 SSM          📧 SNS
                 │
                 ▼
          Restart approved
           FastAPI service
                 │
                 ▼
             ❤️ Health Check
              /          \
             /            \
            ▼              ▼
         ✅ Healthy     ❌ Failed
            │               │
            ▼               ▼
        🗄️ DynamoDB      📧 SNS
          RESOLVED       ESCALATE
```

### What happens in this flow?

| Step | What it does |
|---|---|
| **ALB** | Sends user traffic to healthy EC2 targets and performs health checks. |
| **CloudWatch** | Watches application/infrastructure signals and changes the alarm to `ALARM` when the condition is met. |
| **EventBridge** | Receives the alarm state-change event and invokes Lambda. |
| **Lambda** | Acts as the AIOps controller and coordinates the incident. |
| **SSM** | Collects diagnostics from the EC2 instance and can execute an approved command without SSH. |
| **Bedrock** | Analyzes the incident evidence and returns a structured recommendation. |
| **Safety Gate** | Checks whether the recommended action is allowed before anything changes. |
| **SSM remediation** | Performs the approved EC2 action, such as restarting the application service. |
| **Health Check** | Confirms whether the application actually recovered. |
| **SNS** | Alerts an engineer when the incident should be escalated. |
| **DynamoDB** | Stores the incident, decision, action, and final result. |

---

# ☸️ EKS Flow — AIOps Extension

The EKS version keeps the same **Detect → Diagnose → Reason → Safety → Remediate → Verify** pattern.

```text
                         👤 USER
                            │
                            ▼
                       🌐 AWS ALB
                            │
                            ▼
                      ☸️ EKS Ingress
                            │
                            ▼
                      ☸️ Kubernetes Service
                            │
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
              🐳 Pod-1   🐳 Pod-2   🐳 Pod-3
                ✅         ❌         ✅
                           │
                       Pod failure
                           │
                           ▼
                  📊 Prometheus / Grafana
                    shows the incident
                           │
                           │ automation signal
                           ▼
                    ☁️ CloudWatch Alarm
                           │
                       Alarm = ALARM
                           │
                           ▼
                    ⚡ EventBridge
                           │
                           ▼
                         λ Lambda
                     AIOps Controller
                       /          \
                      /            \
             ☸️ Kubernetes       🧠 Bedrock
               Diagnostics        Reasoning
                      \            /
                       \          /
                        ▼        ▼
                        🛡️ Safety Gate
                         /        \
                        /          \
                       ▼            ▼
                🔧 Remediate    🚨 Escalate
                       │              │
                       ▼              ▼
                ☸️ Kubernetes       📧 SNS
                     API
                       │
                       ▼
               Approved workload
                    action
                       │
                       ▼
                  ❤️ Health Check
                       │
                 ┌─────┴─────┐
                 ▼           ▼
              ✅ Healthy   ❌ Failed
                 │           │
                 ▼           ▼
             🗄️ DynamoDB   📧 SNS
              RESOLVED    ESCALATE
```

> **Important:** Grafana is the visibility/observability layer. The automation trigger in this design is a **CloudWatch alarm state change → EventBridge**. Grafana showing a crashed Pod does not, by itself, invoke EventBridge.

## 🚨 EKS Scenario: Pod Crash / CrashLoopBackOff

A single Pod crash is normally handled by Kubernetes itself:

```text
Pod crashes
    ↓
Deployment / ReplicaSet detects desired replicas are not met
    ↓
Replacement Pod created
    ↓
New Pod becomes Ready
```

Your AIOps layer becomes useful when the failure is **repeated, persistent, or needs diagnosis**:

```text
Pod repeatedly crashes
        ↓
CrashLoopBackOff / restart count increases
        ↓
Prometheus / Grafana shows abnormal behavior
        ↓
CloudWatch alarm reaches ALARM
        ↓
EventBridge
        ↓
Lambda
        ↓
Kubernetes diagnostics
   ├── Pod status
   ├── Restart count
   ├── Container logs
   ├── Kubernetes events
   └── Deployment status
        ↓
Bedrock reasoning
        ↓
Safety Gate
     /       \
    /         \
Remediate   Escalate
    │           │
    ▼           ▼
Kubernetes     SNS
API
    │
    ▼
Approved workload action
    │
    ▼
Health check
    │
    ▼
DynamoDB
```

### Example diagnosis

```text
Observed:
- Pod restart count is increasing
- Container exits repeatedly
- Logs show database connection failures

Bedrock recommendation:
- Root cause: database connectivity issue
- Action: escalate
- Confidence: high

Safety Gate:
- Do not repeatedly restart the Pod
- Send incident to engineer
```

The important idea is **not to restart every crashed Pod blindly**. Kubernetes already handles normal Pod replacement. AIOps is used when the pattern requires deeper diagnosis or a controlled action.

---

# 📈 EKS Scaling Flow

```text
High workload / CPU
        ↓
📊 Kubernetes Metrics
        ↓
☸️ HPA
        ↓
More Pods
        ↓
☸️ Service
        ↓
🌐 ALB
        ↓
✅ Healthy capacity
```

**HPA handles normal Pod scaling. AIOps can analyze unusual behavior such as high CPU + high error rate + abnormal logs.**

---

# 🧠 Common AIOps Decision Flow

```text
1. DETECT
   CloudWatch / Prometheus / Grafana provide the signal

2. ROUTE
   CloudWatch alarm event → EventBridge → Lambda

3. DIAGNOSE
   Lambda collects runtime evidence

4. REASON
   Bedrock analyzes the evidence

5. SAFETY CHECK
   Allow-list and policy checks validate the proposed action

6. ACT
   EC2 → SSM
   EKS → Kubernetes API
   ECS → ECS API (optional)

7. VERIFY
   Health check confirms recovery

8. RECORD / ESCALATE
   DynamoDB stores the result; SNS notifies an engineer when needed
```

---

# 🛡️ Safety Gate

```text
🧠 Bedrock recommendation
          ↓
    🛡️ Safety Gate
          │
     ┌────┴────┐
     ▼         ▼
  Allowed   Not allowed
     │         │
     ▼         ▼
 Execute     SNS
```

Bedrock **recommends** an action. It does not directly get unrestricted infrastructure access.

### Examples of approved actions

```text
EC2 → restart an approved application service through SSM
EKS → restart / roll out an approved workload
EKS → scale an approved workload within limits
ECS → force an approved service deployment
```

### Examples that should normally escalate

```text
IAM permission changes
VPC or routing changes
Broad security-group changes
Infrastructure deletion
Arbitrary AI-generated shell commands
```

---

# 🗄️ Incident Record

Each incident can be stored with information such as:

```json
{
  "incident_id": "INC-00123",
  "runtime": "EKS",
  "resource": "fastapi-deployment",
  "trigger": "pod_failure",
  "severity": "HIGH",
  "root_cause": "Repeated container failures",
  "recommended_action": "restart_workload",
  "confidence": 0.91,
  "safety_decision": "APPROVED",
  "remediation_status": "SUCCESS",
  "health_check": "HEALTHY"
}
```

---

# 📦 Optional ECS Extension

ECS uses the same AIOps control plane with an ECS-specific action layer:

```text
🐳 Docker Image
      ↓
📦 Amazon ECR
      ↓
📦 ECS Service
      ↓
🐳 ECS Tasks
      ↓
🌐 ALB
      ↓
CloudWatch → EventBridge → Lambda → Bedrock → Safety Gate
                                             ↓
                                      ECS API / SNS
                                             ↓
                                        Health Check
                                             ↓
                                         DynamoDB
```

**ECS is optional. The main project story is EC2 + EKS.**

---

# 📁 Project Structure

```text
aws-self-healing-aiops/
├── app/
│   └── fastapi/
├── lambda/
│   └── incident-handler/
├── ec2/
├── eks/
├── monitoring/
├── aiops/
├── terraform/
└── README.md
```

---

# 🚀 Implementation Roadmap

### Phase 1 — EC2 AIOps ✅

```text
EC2 + FastAPI
→ CloudWatch
→ EventBridge
→ Lambda
→ SSM + Bedrock
→ Safety Gate
→ Remediate / SNS
→ Health Check
→ DynamoDB
```

### Phase 2 — EC2 Production Layer

```text
ALB
→ Target Group
→ Auto Scaling Group
→ Multiple EC2 instances
→ FastAPI
```

### Phase 3 — EKS 🚀

```text
EKS
→ Deployment
→ Pods
→ Service
→ Ingress / ALB
→ HPA
→ EKS diagnostics
→ Approved remediation
```

### Phase 4 — ECS 🟡 Optional

```text
Docker
→ ECR
→ ECS Service
→ Tasks
→ ALB
→ Service Auto Scaling
```

---

# 🎯 Project Value

This project demonstrates:

- AWS infrastructure and networking
- EC2, ALB and Auto Scaling
- Docker and Kubernetes
- EKS, Services, Ingress and HPA
- CloudWatch monitoring and EventBridge automation
- Lambda-based incident orchestration
- SSM-based EC2 remediation
- Bedrock-assisted incident reasoning
- Deterministic safety controls
- Health-check-driven recovery verification
- SNS escalation and DynamoDB audit history

## Core Idea

> **AI reasons about the incident. Deterministic rules control the action. AWS/Kubernetes performs the remediation. Health checks verify recovery. DynamoDB records what happened.**
