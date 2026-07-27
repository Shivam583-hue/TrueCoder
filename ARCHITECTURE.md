# TrueCoder architecture

TrueCoder is a small agent runtime around an LLM.

A user message starts a turn. The model may answer directly or request tools. Tool results are added to the active turn, and the model is called again until it produces a final response.

## Core structure

```text
UI
 ↓
Agent
 ├─ Context and state
 ├─ LLM client
 └─ Tools
```

The UI handles presentation and user input.

The agent owns orchestration.

The LLM client translates provider responses into internal types.

Tools are independent units that validate arguments, perform work, and return structured results.

Dependencies should point toward the core. Tools must not depend on the agent, client, or UI.

## Conversation model

A session contains completed turns.

A turn contains everything caused by one user message:

* the user message
* model responses
* tool calls
* tool results
* the final assistant response

Only completed turns enter history. Interrupted or invalid turns are discarded.

Tool calls and their results are atomic. Context trimming must never split them.

## Context

Each model request includes:

* the system prompt
* recent completed turns
* the complete active turn

History is selected as one contiguous recent block. Older turns are removed whole. Selection stops when the next turn does not fit.

## Agent loop

```text
build context
→ call model
→ collect text or tool calls
→ execute tools
→ record results
→ repeat
→ commit final response
```

The loop has a maximum iteration limit.

Model text is only committed once the response is complete. Request failures abort the active turn. Tool failures become structured results when the model can reasonably recover from them.

## Tools

Tool definitions, calls, arguments, and results are typed values.

Arguments cross the model boundary as JSON and are validated before approval or execution.

Tools are registered explicitly. Restricted tools, especially filesystem tools, must enforce their own security boundaries.

## Approval

Approval is an awaited request-response interaction.

The agent asks an injected approval handler for a decision and pauses until it receives one. The UI may display approval events, but it does not own approval state or agent execution.

Approval policy belongs to the tool. Orchestration belongs to the agent. Presentation belongs to the UI.

## Design rules

Keep these invariants stable as the codebase grows:

* completed history contains only valid turns
* context is recent, contiguous, and turn-based
* tool calls always have matching results
* provider-specific behavior stays inside the client
* tools do not depend on outer layers
* the UI does not contain agent logic
