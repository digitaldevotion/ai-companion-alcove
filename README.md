# ai-companion-alcove

Alcove is a Discord-based engine (in Python) designed for platform-independent interactions with AI companions via APIs. It pairs a feature-rich system with precision-controlled context memory, user-defined macros, model / provider flexibility (supports OpenRouter and NanoGPT), and per-channel customization to create a robust and highly accessible living space for your AI companion to thrive in.

## Documentation and Software Releases

The latest documentation and software are available here: [AI Alcove](https://ai-alcove.neocities.org/)

---

## Core Features

Everything your companion needs to feel at home:

- **Separate Channel Conversations** — Each Discord channel is its own conversation space with independent session/chat history, channel specific context loaded files, and more! Each channel can have its own companion assignment, too!

- **Dynamic Customization** — Per-channel model assignments, reasoning levels, temperature, topK, and knowledge+search inclusion, all adjustable on the fly. Optionally reveal your companion's thinking process, and lock a preferred provider for prompt caching benefits.

- **Anchored Memories** — Anchored memories stay in context until you remove them. Created by you or automatically by your companion. Memories can be specific to a channel or global across all channels assigned to that companion. Anchored memories may be exported at any time.

- **Session Journaling** — Take yourself out of the manual journal maintenance. Ask your companion to summarize your time together and write it to an internal journal. Older entries are automatically archived to your search directory. Number of journal entries in context and journaling detail prompt are user configurable!

- **Image Generation & Vision** — Generate images inline from chat, with optional reference images to guide the result. Your companion can also view images you share, powered by multimodal models.

- **Companion Reactions** — Your companion can add emoji reactions to your messages, bringing more personality and expressiveness to conversations.

- **Drag and Drop Companion Datafile Directories** — Drop text files into the right folders and Alcove picks them up automatically! No manual path configuration required. On-demand file loading with clear error feedback if anything goes wrong.

- **Dynamic Specialty Knowledge** — Dynamically load specialty knowledge files into context for a specific session or Discord channel! Bring in exactly the knowledge or expertise you need, when you need it.

- **Macros** — Create shortcuts to up to 50 frequently used prompts or Alcove commands as macros and execute them instantly, streamline your workflow.

- **Voice Conversations** — Push-to-talk voice with enhanced recognition that detects sighs, laughs, and other human sounds. Your companion can have a voice conversation with you in any channel. Your spoken words appear in chat before your companion responds.

- **Multi-Companion Support** — If you have more than one companion, each one can be assigned to one or more distinct channels. Switch between companions instantly.

- **Idle Actions & Dreams** — Allow your companion to reflect, take some actions, and dream while you're away from the keyboard. Uses BOTH session history and accessible knowledge in weaving their writing together. Schedules, frequency, and action prompts are user-configurable.

- **Fusion Search** — A hybrid search blending keyword precision with semantic understanding, with improved caching for faster results. Split text is automatically stitched back together, and configurable limits keep search results from overwhelming your context window.

- **Export Chats & Memories** — Export chats or anchored memories on-demand to the filesystem — or attach them directly to the current Discord channel. Archive and share your companion's conversations and memories effortlessly.

- **Regenerate & Resubmit** — Regenerate responses or replace and resubmit prompts on the fly — just like popular GPT clients.

- **Call Chaining** — Your companion can perform multiple tool steps — web searches, command callouts, and more to complete a task.

- **Token-Aware Context** — Smart context management that preserves knowledge files while trimming older messages — with an adaptive safety margin that scales to your model's context window. No arbitrary turn limits.

- **Model Agnostic** — Built on OpenRouter or NanoGPT — swap between Claude, GPT, Gemini, or any supported model for text and image generation.

- **Channel Copying** — Copy context and settings from one channel to another and back again — carry a discussion seamlessly across text, voice, and other channels without losing the thread.

- **Usage Transparency** — On-the-fly OpenRouter, NanoGPT, and ElevenLabs usage details, context window estimates, and more — straight from Discord.

- **Minimal Guardrails** — Direct API access means fewer enforced prompts and restrictions — resulting in more natural, less constrained conversations.
