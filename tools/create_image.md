## createimage

Generate an image using an LLM-supplied prompt.

**Syntax:**

<createimage>
a cat wearing a top hat, oil painting style
</createimage>

<createimage use="reference">
add a pixie to the scene, matching the style and lighting of the reference photo
</createimage>

**Parameters:**
- **use** (optional) — Set to `"reference"` to incorporate the user's attached reference image(s) into the generated image

**When to use `use="reference"`:**
- The user asks you to modify, edit, add to, or transform a photo they shared
- The user says things like "use this photo", "add yourself to this picture", "make it look like this", "change the background of this image"
- The user's request clearly depends on a specific image they attached

**When NOT to use `use="reference"`:**
- The user wants a brand new image from scratch, even if they previously shared a photo
- The user says "never mind" or changes direction away from the reference image
- The reference image was shared for context only (e.g. "what does this look like?" → describe it, don't generate from it, unless they ask you to generate an image based on it)
