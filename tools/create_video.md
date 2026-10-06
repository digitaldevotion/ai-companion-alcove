## createvideo

Generate a video using an LLM-supplied prompt.

**Syntax:**

<createvideo>
a cat wearing a top hat, oil painting style
</createvideo>

<createvideo use="reference">
add a pixie to the scene, matching the style and lighting of the reference photo
</createvideo>

<createvideo seconds="10">
a long cinematic shot of a ship at sea, dramatic lighting
</createvideo>

<createvideo use="reference" seconds="5">
make the person in this video wave hello
</createvideo>

**Parameters:**
- **use** (optional) — Set to `"reference"` to incorporate the user's attached reference image(s) and/or video(s) into the generated video
- **seconds** (optional) — Duration of the video in seconds (e.g. `"5"`, `"10"`). If omitted, the model's default duration is used. Longer videos cost more.

**When to use `use="reference"`:**
- The user asks you to animate, extend, edit, or transform a photo or video they shared
- The user says things like "make this move", "animate this", "add motion to this video", "extend this clip"
- The user's request clearly depends on a specific image or video they attached

**When NOT to use `use="reference"`:**
- The user wants a brand new video from scratch, even if they previously shared a photo/video
- The user says "never mind" or changes direction away from the reference media
- The reference media was shared for context only

**Notes:**
- Video generation is **much slower** than image generation (minutes, not seconds). Always inform the user that it may take a few minutes.
- Videos over Discord's size limit (~25 MB) are uploaded to a temporary host (litterbox.catbox.moe, auto-deleted after 72 hours) and the link is posted in chat. A copy is always saved locally to `output/videos/`.
- Not all video models support reference images or videos. If the model doesn't support the input type, you'll get an error — try a different model or omit the reference.
