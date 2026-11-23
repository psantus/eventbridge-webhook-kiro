# GitHub webhooks to Kiro CLI or an AgentCore agent, with EventBridge Webhook Events

On November 20, 2026 AWS [announced](https://aws.amazon.com/about-aws/whats-new/2026/11/amazon-eventbridge-webhook-events/) Webhook Events for the enhanced Custom event bus. You create a webhook event source on a bus and EventBridge gives you a managed HTTPS URL. It verifies each request (HMAC signature, static token or basic auth) and publishes accepted requests to the bus, where Subscribers filter, transform and deliver them. There is no API Gateway or Lambda to run. See the [Webhook Events user guide](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-custom-bus-webhook-sources.html).

This repo wires GitHub pull request events to an AI agent in two ways:

```
GitHub (X-Hub-Signature-256)
  | HTTPS POST
  v
EventBridge webhook event source --> Custom event bus
                                       |
              filter: pull_request opened / synchronize / reopened
                                       |
          +----------------------------+------------------------------+
          | A. SQS subscriber (JSONata)                               | B. Universal target
          v                                                           v
   SQS queue (+ DLQ)                                bedrockagentcore:InvokeAgentRuntime
          ^                                                    (any AgentCore agent)
          | long poll
   poll-sqs-kiro.sh --> detached kiro-cli session (resumable)
```

| File | Used by |
|---|---|
| `filter-configuration.json` | both: Subscriber filter |
| `transformer.jsonata` | A: builds the SQS message, including `prompt` |
| `poll-sqs-kiro.sh` | A: consumes the queue and starts Kiro |
| `agentcore/invoke-input.jsonata` | B: builds the `InvokeAgentRuntime` request |
| `agentcore/subscriber-policy.json` | B: delivery role permissions |
| `agentcore/ack-agent/main.py`, `agentcore/ack-agent-*.json` | B: a minimal test agent that only acknowledges the PR |
| `iam/*.json` | role trust and permissions (templates for `envsubst`) |

## What was tested

Tested on AWS CLI 2.42.1, Kiro CLI 3.5.0, in `us-east-1`, on November 23, 2026:

- every `aws eventsv2`, `aws iam` and `aws sqs` command below;
- signed `curl` requests: 200 with a valid signature, 403 with a bad or missing one;
- the filter: `opened`, `synchronize` and `reopened` pass; `closed` and `issues` events are dropped;
- the JSONata transformer, including a login with a quote and a newline (the output stays valid JSON);
- `poll-sqs-kiro.sh` against the real queue: it starts a detached session, acks the message, and the session can be resumed after it finishes;
- a real GitHub hook on a private repo: the `ping` and a `pull_request` `opened` event both got 200 and the PR reached Kiro;
- variant B end to end: a real PR on GitHub reached the test agent in `agentcore/ack-agent`, which logged the repo, PR number, action, delivery ID and the `github-pr-...` session ID.

## Prerequisites

- AWS CLI **2.42.1 or later** (the `aws eventsv2` commands), with rights on EventBridge, SQS, IAM and Secrets Manager
- `jq`, `openssl`, `curl`, `envsubst` (gettext), `gh` for the GitHub step
- Variant A: `kiro-cli` 3.5 or later, logged in (`kiro-cli login`) or with `KIRO_API_KEY`
- Variant B: a deployed AgentCore agent runtime

Note that the new bus is `aws eventsv2`, not `aws events`. `aws events` only manages the classic buses.

## Variables

```bash
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export GH_REPO=OWNER/REPO
```

## 1. Event bus

```bash
export BUS_ARN=$(aws eventsv2 create-event-bus --name kiro-gh-bus \
  --description "GitHub webhooks" --query EventBusArn --output text)
# wait for ACTIVE (about 10 s)
aws eventsv2 describe-event-bus --event-bus-arn "$BUS_ARN" --query State
```

Buses, sources and subscribers are addressed by ARN, not by name.

## 2. Webhook event source

The source's role must trust `events.amazonaws.com` and allow `events:PutWebhookEvents` on the bus and `secretsmanager:GetSecretValue` on the source's secret. Without the secret permission, **every request gets a 403**, even when correctly signed, and nothing is logged.

```bash
aws iam create-role --role-name kiro-gh-webhook-source \
  --assume-role-policy-document "$(envsubst < iam/trust-policy.json)"
aws iam put-role-policy --role-name kiro-gh-webhook-source --policy-name webhook \
  --policy-document "$(envsubst < iam/webhook-source-policy.json)"
sleep 15   # let IAM propagate

aws eventsv2 create-event-source --name github --event-bus-arn "$BUS_ARN" \
  --configuration "{\"WebhookEventsConfiguration\":{
    \"RoleArn\":\"arn:aws:iam::${ACCOUNT_ID}:role/kiro-gh-webhook-source\",
    \"AuthConfiguration\":{\"HmacAuthConfiguration\":{
      \"Algorithm\":\"SHA256\",\"Encoding\":\"HEX\",
      \"SignatureHttpHeader\":\"X-Hub-Signature-256\",\"SignaturePrefix\":\"sha256=\",
      \"SignedMessageComposition\":\"BODY\"}},
    \"MetadataHttpHeaders\":[\"x-github-event\",\"x-github-delivery\"]}}" > source.json

export SOURCE_ARN=$(jq -r .EventSourceArn source.json)
export WEBHOOK_URL=$(jq -r .EventSourceDetails.WebhookDetails.WebhookUrl source.json)
SECRET_ARN=$(jq -r .EventSourceDetails.WebhookDetails.SecretArn source.json)
export WEBHOOK_SECRET=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" \
  --query SecretString --output text)
rm source.json
```

- `MetadataHttpHeaders` copies GitHub's event name and delivery ID into the event's `Metadata`. The body is in `Data`, unchanged.
- Treat the URL as a secret: anyone with the URL and the secret can publish to your bus.
- EventBridge generated the secret here. To set your own, use `aws eventsv2 rotate-webhook-credentials --event-source-arn "$SOURCE_ARN" --credential '{"HmacSecretValue":"..."}'`.

## 3. Queues (variant A) and dead-letter queue (both)

```bash
export DLQ_URL=$(aws sqs create-queue --queue-name kiro-gh-dlq --query QueueUrl --output text)
export DLQ_ARN=arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:kiro-gh-dlq

export QUEUE_URL=$(aws sqs create-queue --queue-name kiro-gh-tasks --attributes "{
  \"ReceiveMessageWaitTimeSeconds\":\"20\",
  \"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
}" --query QueueUrl --output text)
export QUEUE_ARN=arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:kiro-gh-tasks
```

The subscriber delivers with an IAM role, so the queue needs no resource policy.

## 4A. Subscriber to SQS, then Kiro CLI

```bash
aws iam create-role --role-name kiro-gh-subscriber \
  --assume-role-policy-document "$(envsubst < iam/trust-policy.json)"
aws iam put-role-policy --role-name kiro-gh-subscriber --policy-name send-sqs \
  --policy-document "$(envsubst < iam/subscriber-policy.json)"
sleep 15

jq -n --rawfile e transformer.jsonata \
  '{Type:"JSONATA",JsonataConfiguration:{Expression:($e|rtrimstr("\n"))}}' > /tmp/transformer.json

aws eventsv2 create-subscriber --name github-pr-to-kiro --event-bus-arn "$BUS_ARN" \
  --type UNORDERED \
  --invoke-configuration "{\"RoleArn\":\"arn:aws:iam::${ACCOUNT_ID}:role/kiro-gh-subscriber\",\"TargetArn\":\"$QUEUE_ARN\"}" \
  --filter-configuration file://filter-configuration.json \
  --transformer file:///tmp/transformer.json \
  --on-failure-configuration "{\"Arn\":\"$DLQ_ARN\"}"
```

Things the CLI and docs enforce:

- The filter has one pattern per scope, and an event must match all of them. Copied headers are matched in the `METADATA` scope, the body in `DATA`.
- The JSONata expression must be wrapped in `{% %}` with **nothing after `%}`**, not even a newline (hence `rtrimstr`). The event is `$events` (plural).
- JSONata builds the JSON itself, so quotes or newlines in GitHub fields cannot break the message. The PR title and body are still left out of the prompt on purpose: free text from strangers is the main prompt injection path.

### The poller

`poll-sqs-kiro.sh` long polls the queue, one message at a time. For each message it:

1. writes the `prompt` field to `kiro-runs/<messageId>.prompt`;
2. starts `kiro-cli chat --v3 --no-interactive --output-format stream-json` with `nohup`, detached, in `WORKDIR`;
3. waits for the session ID, which is in the first `stream-json` events (normally a few seconds, `SESSION_WAIT` max, default 60 s);
4. deletes the message and appends a line to `kiro-runs/runs.tsv`: time, message ID, session ID, PID, `repo#number action`.

If no session ID shows up within `SESSION_WAIT` seconds, it kills the process and leaves the message on the queue. After 3 receives, SQS moves it to the DLQ. There is no limit on run duration and no limit on how many runs run in parallel.

```bash
export QUEUE_URL                       # from step 3
export WORKDIR=$HOME/src/repo          # checkout of the repo to review
./poll-sqs-kiro.sh                     # ONCE=1 to process a single message
```

Follow a run with `tail -f kiro-runs/<messageId>.jsonl`. The run is over when the last line is `runFinished` or `runError`. Then you can resume it interactively:

```bash
cd "$WORKDIR"                                   # sessions are stored per directory
kiro-cli chat --v3 --resume-id <sessionId>      # from kiro-runs/runs.tsv
```

While the run is still going, a resume fails with `SessionHeldElsewhereError`, because the headless process owns the session.

The default `KIRO_TRUST_TOOLS=read,grep_search` lets Kiro read and search files only. `read` is the V2 name for the read capability and also covers listing; with only `read_file`, Kiro could not list the checkout in the GitHub test. The script does not check out the PR branch, so Kiro sees `WORKDIR` as it is. Add a `gh pr checkout` step, and a matching tool trust, if you want it to see the diff.

Since the message is acked when the session starts, a run that fails later is not retried. Its `.jsonl` keeps the error.

## 4B. Subscriber to an AgentCore agent (universal target)

A universal target calls an AWS API directly from the subscriber: here `InvokeAgentRuntime` on any AgentCore agent runtime, with no queue or poller.

```bash
export AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/my_agent-XXXXXXXXXX

aws iam create-role --role-name kiro-gh-agentcore-subscriber \
  --assume-role-policy-document "$(envsubst < iam/trust-policy.json)"
aws iam put-role-policy --role-name kiro-gh-agentcore-subscriber --policy-name invoke \
  --policy-document "$(envsubst < agentcore/subscriber-policy.json)"
sleep 15

# only substitute AGENT_RUNTIME_ARN: the expression also contains $events
INPUT=$(envsubst '${AGENT_RUNTIME_ARN}' < agentcore/invoke-input.jsonata)
jq -n --arg role "arn:aws:iam::${ACCOUNT_ID}:role/kiro-gh-agentcore-subscriber" --arg in "$INPUT" \
  '{TargetArn:"arn:aws:events:::aws-sdk:bedrockagentcore:invokeAgentRuntime", RoleArn:$role,
    UniversalTargetParameters:{Input:($in|rtrimstr("\n")), InvocationTimeoutSeconds:"30"}}' \
  > /tmp/agentcore-invoke.json

aws eventsv2 create-subscriber --name github-pr-to-agentcore --event-bus-arn "$BUS_ARN" \
  --type UNORDERED \
  --invoke-configuration file:///tmp/agentcore-invoke.json \
  --filter-configuration file://filter-configuration.json \
  --batch-configuration MaxBatchSize=1,MaxBatchWindowInSeconds=0 \
  --on-failure-configuration "{\"Arn\":\"$DLQ_ARN\"}"
```

### A test agent

`agentcore/ack-agent/main.py` is a standard-library Python agent. It serves the AgentCore HTTP contract (`GET /ping`, `POST /invocations` on port 8080), logs one JSON line per PR and returns an ack. It is deployed as code from S3, so you don't need to build a container:

```bash
export CODE_BUCKET=my-agent-code-${ACCOUNT_ID}
aws s3api create-bucket --bucket "$CODE_BUCKET"
(cd agentcore/ack-agent && zip -q /tmp/ack-agent.zip main.py)
aws s3 cp /tmp/ack-agent.zip "s3://$CODE_BUCKET/ack-agent.zip"

aws iam create-role --role-name kiro-gh-ack-agent \
  --assume-role-policy-document "$(envsubst < agentcore/ack-agent-trust.json)"
aws iam put-role-policy --role-name kiro-gh-ack-agent --policy-name runtime \
  --policy-document "$(envsubst < agentcore/ack-agent-policy.json)"
sleep 20   # a create right after the role often fails with AccessDenied

export AGENT_RUNTIME_ARN=$(aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name kiro_gh_ack_agent \
  --agent-runtime-artifact "{\"codeConfiguration\":{\"code\":{\"s3\":{\"bucket\":\"$CODE_BUCKET\",\"prefix\":\"ack-agent.zip\"}},\"runtime\":\"PYTHON_3_13\",\"entryPoint\":[\"main.py\"]}}" \
  --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/kiro-gh-ack-agent" \
  --network-configuration networkMode=PUBLIC --protocol-configuration serverProtocol=HTTP \
  --query agentRuntimeArn --output text)
```

After a PR event, its acks are in the log group `/aws/bedrock-agentcore/runtimes/kiro_gh_ack_agent-<id>-DEFAULT`.

The agent must accept IAM (SigV4) calls. An agent with a `customJWTAuthorizer` (OAuth) rejects EventBridge's calls.

What the request contains (`agentcore/invoke-input.jsonata`):

- `Payload`: a JSON string with `repo`, `number`, `url`, `author`, `action`, `delivery` and `prompt`. Adapt it to what your agent's entrypoint reads.
- `RuntimeSessionId`: `github-pr-` plus 32 hex characters of a SHA-256 of `repo#number`. Every event of the same PR lands in the same AgentCore session, and the ID meets the 33-character minimum.
- Request members use the API's PascalCase names (`AgentRuntimeArn`, `Payload`, ...). With camelCase names, delivery fails with `CUSTOMER_VALIDATION ... missing required field(s)`.
- A universal target batches events, and `$events` is an array, hence `$events[0]` and `MaxBatchSize=1`.

Limits to design around:

- `InvocationTimeoutSeconds` is 30 s max. An agent that streams a long answer before returning will time out, be retried (5 attempts in 300 s by default) and run several times. The agent should accept the request, start the work as an async task, and return right away.
- Failures land in the DLQ as a record with `errorCode` and `errorMessage` (for example `CUSTOMER_VALIDATION`, `ACCESS_DENIED`, `RESOURCE_NOT_FOUND`). That is the first place to look.
- The agent's answer goes nowhere: EventBridge ignores the response. The agent has to post its review itself, for example as a GitHub comment.

## 5. Point GitHub at the URL

```bash
jq -n '{name:"web", active:true, events:["pull_request"],
        config:{url:env.WEBHOOK_URL, content_type:"json", secret:env.WEBHOOK_SECRET}}' |
  gh api "repos/$GH_REPO/hooks" --method POST --input - --jq .id
```

The secret goes through stdin, not the command line. GitHub sends a `ping` event when the hook is created. The filter drops it, and the source answers 200.

## 6. Test without GitHub

```bash
BODY='{"action":"opened","number":7,"repository":{"full_name":"OWNER/REPO"},"pull_request":{"html_url":"https://github.com/OWNER/REPO/pull/7","user":{"login":"octocat"}}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print $NF}')"
curl -s -w ' %{http_code}\n' -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' -H 'X-GitHub-Event: pull_request' \
  -H 'X-GitHub-Delivery: test-1' -H "X-Hub-Signature-256: $SIG" --data-binary "$BODY"

aws sqs receive-message --queue-url "$QUEUE_URL" --wait-time-seconds 10 --visibility-timeout 0
```

| Answer | Meaning |
|---|---|
| 200 | accepted and published (or dropped as a duplicate if deduplication is on) |
| 403 | bad signature, or the source role cannot read the secret |
| 404 | wrong URL, or the source or bus is gone |

To see what the bus really delivers, add a temporary subscriber with `--transformer '{"Type":"WITH_METADATA"}'`. It delivers `Data`, `Metadata` and `SystemMetadata`.

## Acting on the repository

A GitHub webhook carries no token. To comment on the PR, use a GitHub App: its webhooks include `installation.id`, and the app exchanges its private key for a 1-hour installation token (`POST /app/installations/{id}/access_tokens`). Alternatively, keep a fine-grained PAT in Secrets Manager and let the agent's role read it.

## Security

- **Prompt injection.** PR content comes from strangers. Keep tool trust minimal, never `--trust-all-tools` on a machine with credentials, and run in a throwaway checkout or container.
- **Secrets.** The webhook URL and secret are sensitive. The webhook secret stays in Secrets Manager and environment variables, never in the repo. `kiro-cli` and its tools can read your environment.
- **Duplicates.** GitHub retries, and SQS and EventBridge deliver at least once. A repeated delivery starts another run. Add `"DeduplicationConfiguration":{"DeduplicationType":"CONTENT_BASED"}` to the source to drop identical requests within 5 minutes.
- **Least privilege.** Each role is limited to one bus, one secret prefix, or one queue or agent.

## Cost

Webhook events are billed as Custom event bus ingress, with no webhook surcharge. SQS, Secrets Manager (one secret per source), AgentCore and Kiro are billed separately.

## Cleanup

```bash
for s in $(aws eventsv2 list-subscribers --event-bus-arn "$BUS_ARN" --query 'Subscribers[].SubscriberArn' --output text); do
  aws eventsv2 delete-subscriber --subscriber-arn "$s"; done
aws eventsv2 delete-event-source --event-source-arn "$SOURCE_ARN"
aws eventsv2 delete-event-bus --event-bus-arn "$BUS_ARN"
aws sqs delete-queue --queue-url "$QUEUE_URL"; aws sqs delete-queue --queue-url "$DLQ_URL"
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "${AGENT_RUNTIME_ARN##*/}"   # test agent, if created
for r in kiro-gh-webhook-source kiro-gh-subscriber kiro-gh-agentcore-subscriber kiro-gh-ack-agent; do
  for p in $(aws iam list-role-policies --role-name $r --query PolicyNames --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name $r --policy-name $p; done
  aws iam delete-role --role-name $r 2>/dev/null; done
# and delete the hook in GitHub: gh api repos/$GH_REPO/hooks
```
