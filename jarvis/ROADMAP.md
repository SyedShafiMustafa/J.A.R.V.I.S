# JARVIS V2 — MASTER ROADMAP (SOURCE OF TRUTH)

Source of truth for the entire project. When the README, docs, or
commits disagree with this file, this file wins.

## Production setup

**Machine:** Lenovo IdeaPad Slim 3 (i7, 16GB RAM). Only machine.
**Dell:** Retired. No features built around Dell, Tailscale, or any
two-machine architecture.

The production pipeline:

```text
User
 ↓
Microphone
 ↓
Voice Activity Detection
 ↓
Faster-Whisper
 ↓
Brain Service
 ↓
Ollama (qwen2.5:3b)
 ↓
Text-to-Speech
 ↓
Speakers
```

## Phase map

| Phase    | Objective                     | Status         |
| -------- | ----------------------------- | -------------- |
| Phase 0  | Infrastructure & Backend      | ✅ Complete     |
| Phase 1  | AI Brain                      | ✅ Complete     |
| Phase 2  | Human Voice                   | 🔨 In Progress |
| Phase 3  | Conversational Memory         | 🔨 Partial      |
| Phase 4  | Personal Knowledge            | 🔨 Partial      |
| Phase 5  | Agent Tool System             | 🔨 Partial      |
| Phase 6  | Desktop Application           | 🔨 Partial      |
| Phase 7  | Android Companion             | ⏳            |
| Phase 8  | Computer Automation           | 🔨 Partial      |
| Phase 9  | Communication Suite           | ⏳            |
| Phase 10 | Autonomous Multi-Agent JARVIS | ⏳            |

Phases run in order. The current active phase remains the highest priority.
Later phases should not become the primary development focus before the active phase is complete.
Independent, low-risk foundational work for later phases may be implemented early when it does not destabilize or interfere with the active phase.
Every such early milestone must remain clearly labeled as early foundation work.

## Execution queue

Follow in order. Each item is a milestone with a clear exit criterion.

### P1 — Phase 2.5.3 — Real Voice Loop Validation

Validate the microphone → Whisper → Brain → TTS pipeline end-to-end.

- [ ] Run 3 consecutive conversations
- [ ] Measure first spoken sentence latency
- [ ] Confirm no cold loading between turns
- [ ] Confirm TTS reliability across all turns
- [ ] Verify VAD ignores silence
- [ ] Greeting begins speaking in under 3 seconds
- [ ] Medium question completes in under 6 seconds
- [ ] No fake transcripts
- [ ] No TTS failures

Exit criterion: the user can speak three real turns to JARVIS and get
reliable replies with no manual intervention.

### P2 — Phase 2.6 — Wake Word

Keyboard-free activation.

- [x] "Jarvis" wake word detection (commit: c5ae5cd)
- [ ] Activation without touching the keyboard (hardware validation pending)

### P3 — Phase 2.7 — Barge-In

Natural turn-taking.

- [x] User can interrupt JARVIS while speaking (commit: 631866e)
- [ ] Interrupt is handled cleanly (hardware validation pending)

### P4 — Phase 2.8 — Always Listening Mode

Background desktop assistant.

- [x] Idle until wake word (commit: c5ae5cd)
- [ ] Background assistant behavior works (hardware validation pending)

### P5 — Phase 3 — Conversational Memory

Follow-up conversations feel human.

- [x] Conversation IDs (commit: f4cd7cc)
- [x] Last 10–20 turns retained (commit: f4cd7cc)
- [x] SQLite conversation history (commit: f4cd7cc)
- [x] Context injection (commit: 2142cfa)
- [x] Session restoration (commit: f4cd7cc)
- [x] Runtime conversation summarization (commit: b93d405)
- [x] Automatic conversation summarization (commit: c40401b)

Not included in this phase: vector databases, embeddings, RAG. Those
belong to Phase 4.

Note: Phase 3 memory infrastructure is largely complete but may require
integration testing with Phase 2 voice features.

### P6 — Phase 4 — Personal Knowledge

Persistent long-term memory.

- [x] Documents (V4.1A parser foundation - commit: 88d5779)
- [x] Document chunking (V4.1B chunking engine - commit: f6403bf)
- [x] SQLite knowledge persistence (V4.1C storage layer - commit: 706e581)
- [ ] User preferences
- [ ] Projects
- [ ] PDF retrieval (parsers support PDF, retrieval not yet implemented)
- [ ] Embeddings
- [ ] Semantic search
- [ ] RAG integration
- [ ] Persistent knowledge across days

Note: V4.1A/B/C provide solid foundation for document parsing, chunking,
and persistence. Remaining work includes embeddings, semantic search, and
RAG integration.

### P7 — Phase 5 — Agent Tool System

JARVIS performs real tasks.

- [x] Computer control tool (commit: multiple)
- [x] Desktop automation tool (commit: multiple)
- [x] UI automation tool (commit: multiple)
- [x] Vision tool (commit: multiple)
- [x] Tool executor (commit: multiple)
- [x] Planner agent (commit: multiple)
- [ ] File manager tool
- [ ] Browser tool
- [ ] Terminal tool
- [ ] Calculator tool
- [ ] Calendar tool
- [ ] Notes tool
- [ ] Weather tool
- [ ] Complete planner → tool selection → execution → response flow
- [ ] Every tool remains modular

Note: Basic tool infrastructure exists (computer control, UI automation, vision,
executor, planner) but full tool ecosystem and integration workflow incomplete.

### P8 — Phase 6 — Desktop Application

Replace development scripts with a native application.

- [x] React frontend skeleton (commit: 9a7281e)
- [x] Backend API integration (commit: 9a7281e)
- [ ] System tray
- [ ] Push-to-talk
- [ ] Conversation history
- [ ] Settings
- [ ] Audio controls
- [ ] Model selector
- [ ] Background service

Note: Basic React frontend exists but is a skeleton. Full desktop application
features (system tray, push-to-talk, settings, etc.) not yet implemented.

### P9 — Phase 7 — Android Companion

Connect Android directly to Lenovo.

- [ ] Voice chat
- [ ] Notifications
- [ ] Remote commands
- [ ] Clipboard sync
- [ ] Camera input
- [ ] Optional location awareness
- [ ] No cloud dependency

### P10 — Phase 8 — Computer Automation

JARVIS operates Windows.

- [x] Basic computer control (commit: multiple)
- [x] Desktop automation (commit: multiple)
- [x] UI automation (commit: multiple)
- [x] Vision capabilities (commit: multiple)
- [ ] Launch applications
- [ ] VS Code workflows
- [ ] Browser automation
- [ ] File organization
- [ ] Multi-step desktop tasks

Example: *"Jarvis, pull my GitHub repository and start my development
environment."*

Note: Core automation capabilities exist (computer control, UI automation, vision)
but high-level workflow automation and application-specific integrations incomplete.

### P11 — Phase 9 — Communication Suite

External communication.

- [ ] Email
- [ ] Discord
- [ ] Telegram
- [ ] WhatsApp (where permitted)
- [ ] SIP phone calls
- [ ] Call summaries
- [ ] Security and authentication are mandatory

### P12 — Phase 10 — Autonomous Multi-Agent JARVIS

Final architecture.

```text
                JARVIS
                   │
          Planning Agent
         ╱      │      ╲
    Memory   Tool Agent  Communication
         ╲      │      ╱
        Response Generator
                   │
              Voice Output
```

- [ ] Long-term memory
- [ ] Multi-step planning
- [ ] Autonomous tool execution
- [ ] Background tasks
- [ ] Personal preference learning
- [ ] Proactive assistance

## Project rules

1. Lenovo is the only production machine.
2. Dell is retired.
3. Finish the current phase before starting the next.
4. Every milestone must be measurable and manually verified.
5. No feature creep — Android, telephony, agents, and communication
   begin only after Phase 2 is complete.
6. Keep commits focused: one milestone per commit.
7. Always report benchmark results, modified files, and commit hash
   after completing a milestone.
