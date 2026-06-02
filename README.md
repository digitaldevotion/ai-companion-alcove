# ai-companion-alcove

A Discord-based engine (in Python) designed for platform-independent interactions with AI companions via APIs. It pairs a feature-rich system with precision-controlled context memory, user-defined macros, model / provider flexibility (supports OpenRouter and NanoGPT), and per-channel customization to create a robust and highly accessible living space for your AI companion to thrive in.

## Documentation and Software Releases

The latest documentation and software are available here: [AI Alcove](https://www.google.com/search?q=https://ai-alcove.neocities.org/)

---

## Core Features

Everything your companion needs to feel at home:

### 💬 Conversations & Interaction

* **Channel Conversations:** Each Discord channel is its own conversation space with independent chat history, model assignments, and context settings.
* **Voice Conversations:** Push-to-talk voice support via ElevenLabs. Your companion joins voice channels, listens, and speaks its responses aloud.
* **Image Generation & Vision:** Generate images inline from chat. Your companion can also view images you share, powered by multimodal models.
* **Companion Reactions:** Your companion can add emoji reactions to your messages, bringing more personality and expressiveness to conversations.
* **Export Chats:** Export chats on-demand to the filesystem or directly attached to the current conversation — archive and share your companion's conversations effortlessly.

### 🧠 Memory & Context Management

* **Persistent Memory:** Global and channel-specific anchored memories stay in context until you remove them. Created manually by you or automatically by your companion. Your companion remembers what matters.
* **Cross-Channel Context:** Copy context from one channel to another and back again — carry a discussion seamlessly across text, voice, and other channels without losing the thread.
* **Token-Aware Context:** Dynamic token budgeting trims oldest messages first while preserving knowledge files. Smart context, not arbitrary turn limits.
* **Regenerate & Resubmit:** Regenerate responses or replace and resubmit prompts on the fly — just like popular GPT clients.

### 📚 Knowledge & Search

* **Fusion Search:** A hybrid search algorithm that combines keyword precision with semantic recall. Smart neighbor recovery stitches together text split across chunk boundaries, and dynamic budgeting scales results to fill available context.
* **Auto-Loading Datafile Directories:** Drop text files related to your companion into the right folders and Alcove picks them up automatically — no manual path configurations required.
* **Dynamic Specialty Knowledge:** Dynamically load specialty knowledge files into context for a specific session or Discord channel — bring in exactly the expertise you need, when you need it.

### ⚙️ Customization, Models & Logic

* **Dynamic Customization:** Per-channel LLM model assignments, context window limits, reasoning levels, and knowledge inclusion — all adjustable on the fly.
* **Model Agnostic:** Built on OpenRouter or NanoGPT — swap between Claude, GPT, Gemini, or any supported model for text and image generation.
* **Macros:** Save frequently used prompts or Alcove commands as macros and execute them instantly — streamline your workflow.
* **Call Chaining:** Your companion can now perform multiple tool steps — web searches, command callouts (if enabled), and more — to complete a task.

### 🔒 Transparency & Control

* **Usage Transparency:** On-the-fly OpenRouter, NanoGPT, and ElevenLabs usage details, context window estimates, and more — straight from Discord.
* **Minimal Guardrails:** Consumer-level prompt enforcement and safe completions are minimized through direct API access. A more natural conversation.