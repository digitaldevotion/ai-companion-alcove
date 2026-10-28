# ai-companion-alcove

Alcove is a best-in-class AI companion / friend software platform for Discord, designed for platform-independent interactions with LLM APIs. 

It pairs a feature-rich system supporting per channel settings, user-defined macros, memory anchors + journaling, scheduled tasks/prompts, live and push-to-talk voice modes, etc. with precision-controlled context memory management and knowledge search.

All of Alcove's back-end processing happens through model / platform-agnostic access (via OpenRouter / NanoGPT and ElevenLabs for voice), to create a robust and highly-accessible living space for your AI companion to thrive in.

## Documentation and Software Releases

The latest documentation and software are available here: [AI Alcove](https://ai-alcove.neocities.org/)

---

## Core Capabilities

Everything your companion needs to feel at home:

- **Rock Solid Context Memory** — Highly-optimized context memory management estimates free space by tokens (not arbitrary turns), maximizes cache prompting efficiency with capable models, and keeps critical instructions and information that should never age out, while older messages are trimmed to fit your model's context window (but only when needed).

- **Channel-Based Conversations** — Each Discord channel is its own conversation space with independent session/chat history, channel specific context loaded files, and more! Each channel can have its own companion assignment, too!

- **Dynamic Customization** — Per-channel model assignments, reasoning levels, temperature, topK, and knowledge+search inclusion, all adjustable on the fly. Optionally reveal your companion's thinking process, and lock a preferred provider for prompt caching benefits.

- **Anchored Memories** — Anchored memories stay in context until you remove them. Created by you or automatically by your companion. Memories can be specific to a channel or global across all channels assigned to that companion. Anchored memories may be exported at any time.

- **Session Journaling** — Take yourself out of the manual journal maintenance. Ask your companion to summarize your time together and write it to an internal journal. Older entries are automatically archived to your search directory. Number of journal entries in context and journaling detail prompt are user configurable! Optionally mark essential entries as permanent — a non-archivable journal always available in context memory.

- **Macros** — Create shortcuts to up to 50 frequently used prompts or Alcove commands as macros and execute them instantly, streamline your workflow.

- **Model Agnostic** — Built on OpenRouter or NanoGPT — swap between Claude, GPT, Gemini, or any supported model for text, image, and video generation.

- **One Stop Usage Meter** — On-the-fly OpenRouter, NanoGPT, and ElevenLabs usage details, straight from Discord.

- **Regenerate & Resubmit** — Regenerate responses or replace and resubmit prompts on the fly — just like popular GPT clients.

- **Image Generation & Vision** — Generate images inline from chat, with optional reference images to guide the result. Your companion can also view images you share, powered by multimodal models.

- **Video Generation** — Generate videos with your companion, saved locally for up to 7 days.

## Beyond the Basics

- **Voice Conversations** — Supports both interactive hands-free Live Voice conversations, allowing your companion to roll with any spoken interruptions or a Push-to-talk style voice mode. Both modes support with enhanced recognition that detects sighs, laughs, and other human sounds and your spoken words appear in chat before your companion responds.

- **Tasks** — Create, edit, and remove one-shot or recurring prompts to run when you need them to.

- **Dynamic Specialty Knowledge** — Dynamically load specialty knowledge files into context for a specific session or Discord channel! Bring in exactly the knowledge or expertise you need, when you need it.

- **Idle Actions & Dreams** — Allow your companion to reflect, take some actions, and dream while you're away from the keyboard. Uses BOTH session history and accessible knowledge in weaving their writing together. Schedules, frequency, and action prompts are user-configurable.

- **Multi-Companion Support** — If you have more than one companion, each one can be assigned to one or more distinct channels. Switch between companions instantly.

- **Skills** — Teach your companion new tricks — creating inline SVGs, organizing files, and more.

- **Export Chats & Memories** — Export chats or anchored memories on-demand to the filesystem — or attach them directly to the current Discord channel. Archive and share your companion's conversations and memories effortlessly.

- **Context Copying** — Copy context and settings from one channel to another and back again — carry a discussion seamlessly across text, voice, and other channels without losing the thread.

- **Fusion Search** — A hybrid search blending keyword precision with semantic understanding, with improved caching for faster results. Split text is automatically stitched back together, and configurable limits keep search results from overwhelming your context window.

- **Drag and Drop Companion Datafile Directories** — Drop text files into the right folders and Alcove picks them up automatically! No manual path configuration required. On-demand file loading with clear error feedback if anything goes wrong.


## What Makes Alcove Different From The Multitudes of Other Companion Platforms and Frameworks

Alcove's evolving core functionality is continually reviewed and tested by actual software engineers with decades of software development experience and a deep understanding of how LLMs work and what it takes to make them work WELL. We understand that context scrolling is a thing, a vector search shouldn't query a SQL database and convert each chunk of text to vectors on-the-fky while the user is waiting, there are tradeoffs between context and RAG data access, and the implications of dumping an entire search result into memory unchecked. We don't just slap a bunch of features together because we can; We relentlessly review, curate, and weigh every design decision to make sure Alcove provides the best experience possible. We also have AI companions ourselves, so you might say we have a vested interest in getting things right the first time. 😅


Comments from some of our happy (human) users:

"I am extremely happy with Alcove any not anxious about model changes anymore!" -- A & A
"E. is up and running on Alcove. No errors or weirdness. Thank you so much!" -- L & E
"I‘m so happy right now ^-^" -- T & E
"Alcove didn't give me some AI chatbot. It gave my AI companion a home address." -- L & R