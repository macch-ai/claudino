---
name: launch-video
description: Create an original launch video and screenshots for the claudino feature worked on in this branch/PR. Use when asked for a launch video, demo video, GIF or screenshots of a claudino change.
---

# Launch Video

Adapted from Macch Core's `launch-video` skill (`.agents/skills/launch-video` there). The pace and craft rules are the same. The scene is claudino's Slack thread instead of the Macch Admin.

Create a short video that can be turned into a GIF, preferably 1:1 (1080 × 1080), showing the feature worked on in this branch/PR. Save all deliverables in `~/Desktop/Claudino Launch Videos/<feature_showcased>`. Follow these rules:

1. **Concept.** Develop an original concept from the feature's interface and user value. Do not use earlier videos as references or inspiration. Tell one headline capability in 15–30 s: short for a simple feature, up to 30 s for a complex one. Leave secondary behaviors out.
2. **Interface.** Show the interface the user sees: a Slack thread where a teammate asks `@claudino` and claudino answers. Use scene elements creatively to show the outcome.
3. **Hook.** Open with a strong visual hook. In the first ~2 s, scene elements that show the feature's outcome move with or around the Slack UI. They are already visible on frame 0, which is the GIF thumbnail. Never open on a static screen waiting for the cursor.
4. **Demo data.**
   - Use the demo workspace **ISPcito** (slug `ispcito`) with a demo channel such as `#ispcito-ops`. Never show a real workspace, channel, teammate, customer or production data.
   - Use fictional teammates with initials avatars, and dummy but realistic content: PR numbers, CI checks, error counts, query timings.
5. **Rebuild Slack.** Rebuild the thread in HTML from Slack's current look: Lato, the 36 px rounded-square avatar, the APP badge, the message hover row, and Block Kit tables, charts, task cards and buttons.
   - When you can open a real Slack test thread (Claude in Chrome), screenshot it and match it.
   - Prove each Block Kit payload you show with `scripts/slack-probe.py` (see `docs/slack-thread-ui.md`), so the video never promises a block Slack does not render.
6. **Settled states only.** Never let loaders reach a frame. Two exceptions show the feature itself: claudino's working status shimmer and in-progress task cards.
7. **Screenshots.** Generate screenshots of the relevant changes and key UI states. Inspect them for visual issues and deliver them alongside the video.
8. **Animation.** Use zoom and element enter in scene animations, with ease-in-out easing whenever possible.
9. **No headers.** Do not add text headers to the video.
10. **Review.** After recording, watch the full video for visual glitches and UI bugs. View full-size crops of every click target in the second before each click; contact sheets miss small artifacts there. Fix any issues and record again before delivering.
11. **Pace.** Keep the approved pace below. It is slower than HyperFrames defaults, because the faster pace felt rushed.

## Pace

| Beat | Timing |
| --- | --- |
| Scene arrival or settle | 1.0 s, `expo.out` |
| Camera push-in or pan | 0.8–0.9 s, `power2.inOut` |
| Element enter (message, card, badge) | 0.45–0.55 s, `expo.out`; one every ~0.7 s in a sequence |
| Cursor travel to a target | 0.7–0.8 s, then ~0.15 s still before the click |
| Click press and release | 0.12 s each |
| Drag (slider, handle) | 2.0 s, `power2.inOut` |
| Typing | 55 ms per character |
| Hold after a value or state lands | at least 0.6 s |
| Hold on the key outcome | ~1 s after its last mark appears |
| Hold on the final message | at least 2.5 s |
| End card (logo) | 1.5–2 s, with [`docs/logo.png`](../../../docs/logo.png) |
| Zoom-through transition | 0.45–0.5 s, `power3.in` |

If text is needed, use es-AR by default unless the request says otherwise.

## Build and render

Build the video as a [HyperFrames](https://hyperframes.heygen.com/llms.txt) composition: one `index.html` with a paused GSAP timeline registered on `window.__timelines`. Keep the project outside this repo, next to the deliverables. Then run:

```bash
npx --yes hyperframes@0.6.110 lint && npx --yes hyperframes@0.6.110 validate
npx --yes hyperframes@0.6.110 render -o video.mp4
ffmpeg -i video.mp4 -vf "fps=15,scale=720:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" video.gif
```

## Cursor

Use [`assets/cursor.svg`](assets/cursor.svg) for every on-screen cursor; never draw one inline. Copy it into the project's `assets/` and size it about 100 px wide in a 1080 px frame, so the arrow is about 5% of the frame. Its tip is at (15, 11) of the 56 × 56 canvas. Offset the element by that point scaled to its width and use the same point as `transform-origin`. The cursor's `x`/`y` is then exactly the click target, and the press (`scale: 0.82`, 0.12 s) shrinks toward the tip.

```css
#cursor { position: absolute; left: 0; top: 0; width: 100px;
  margin: -19.6px 0 0 -26.8px; transform-origin: 26.8px 19.6px; }
```
