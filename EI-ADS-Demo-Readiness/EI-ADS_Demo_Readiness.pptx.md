## Slide 1

SOLUTION INTRODUCTION · EI-ADS

AI-Enabled Employment Insurance Fraud Detection

Real-time AI fraud detection across Employment Insurance, Old Age Security and Canada Pension Plan benefits.

Challenge

AI-Enabled Employment Insurance Fraud Detection

Solicitation

EI-ADS

Classification

Protected B

HustleBench Advisory Services Inc. · EI-ADS

## Slide 2

HOW THIS SESSION RUNS

Four movements, and the last one is a conversation

01

Orient

What problem this solves, the detection architecture, and the Requirements & Design grounding.

02

Walk the architecture

Intake to Kafka to Orchestrator to Six Engines to Ensemble Scorer to Investigator Dashboard; Requirements and Design are authoritative.

03

Watch it run

Live full-stack demo and static HTML demos; nothing is faked.

04

Agree what we need

Validation scenarios, readiness and next steps for the client\'s existing systems.

## Slide 3

THE SITUATION

Three constraints that compound

Manual / legacy claim handling

Claims today flow through manual and legacy review workflows rather than an AI detection pipeline.

Fragmented detection capability

Six engines, adapters and a scorer must be wired deliberately into one orchestrated pipeline.

Degraded data and sources

External data sources can be unavailable; engines degrade honestly rather than fabricate confidence.

## Slide 4

THE PROPOSITION

Detect. Explain. Decide.

Real-time AI fraud detection across EI, OAS and CPP --- six parallel engines, ensemble scoring, SHAP explainability and bilingual output.

HustleBench Advisory Services Inc. · EI-ADS

## Slide 5

VIEW 01 --- THE ARCHITECTURE

Intake to Kafka to Orchestrator to Six Engines to Ensemble Scorer to Investigator Dashboard

![ei_ads_component_view.png](ppt/media/image1.png "Picture 3")

A claim flows from claimant intake and ROE ingestion through the detection pipeline into scored outcomes for investigator review.

## Slide 6

VIEW 02 --- THE ARCHITECTURE

Layers and seams of the detection platform

![ei_ads_information_model_view.png](ppt/media/image2.png "Picture 3")

External data adapters feed detection engines; the orchestrator sequences them; the ensemble scorer aggregates Indicators with SHAP attribution.

## Slide 7

THE DECISION RULES

Four tenets that explain most of the design

01

Parallel by design

Six Detection_Engines run concurrently; the Ensemble Scorer aggregates Indicators into a Risk_Score with SHAP attribution.

02

Honest degradation

Engine or data-source failure yields PARTIAL_RESULT or INDETERMINATE; payment is halted rather than guessing.

03

Explainable outcomes

Every Risk_Decision ships with SHAP attributions and a bilingual LLM explanation.

04

Governed handling

Protected B, GCKey auth, immutable audit trail, bilingual output.

## Slide 8

VIEW 03 --- THE TECHNOLOGY VIEW

Technology stack of the AI-enabled fraud detection system

![ei_ads_technology_view.png](ppt/media/image3.png "Picture 3")

Python services and React portal, Apache Kafka event backbone, PostgreSQL and ClickHouse analytical stores, Neo4j graph store, Llama 3.1 LLM, XGBoost and Isolation Forest scoring --- deployed on Protected B cloud.

## Slide 9

VIEW 04 --- DEPLOYMENT & GOVERNMENT CONTEXT

Built for the Government of Canada context

![ei_ads_context_view.png](ppt/media/image4.png "Picture 3")

Protected B classification, GCKey OAuth 2.0, Privacy Act and PIPEDA compliance, bilingual output and immutable audit.

## Slide 10

WHAT WE HAVE MEASURED

Evidence of a working solution

All six Detection_Engines composed and integration-tested over real transports (Kafka, real HTTP, GCKey JWKS).

Ensemble Scorer wiring complete; real ML models pending.

Unit, property-based (P1-P15) and integration evidence assembled; live compose verified across slices.

## Slide 11

WHERE THE EDGES ARE

What we are deliberately not claiming today

Ensemble real ML models pending.

Transit and economic signals, and document-registry sources, still to be fed into the pipeline.

Client-specific validation scenarios pending.

## Slide 12

DEMONSTRATION - WHAT YOU ARE ABOUT TO WATCH

Six acts. Nothing is faked.

Every output comes from the real pipeline, the static demos, or the verification harness; which parts are live and which are test harnesses is stated openly.

## Slide 13

WHAT WE NEED FROM YOU

The readiness package for your existing systems

Implementation verified against Requirements and Design; package supports your team evaluating integration with existing government systems.

## Slide 14

NEXT STEPS & Q&A

Next steps and discussion

Real ensemble models, Kubernetes/OpenShift deployment, and client validation scenarios.
