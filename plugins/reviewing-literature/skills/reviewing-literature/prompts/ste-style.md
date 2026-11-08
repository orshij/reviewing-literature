# Writing style: ASD-STE100, applied about 80% of the way (step 6)

Every text a reader sees in the review's Explainers tab uses this style: cluster names and distinguishing traits,
the slide's headline, sections, details, glossary and figure reason, the critique's limitations, weak points and
their rationale, and the synthesis takeaways and their details. Quotes stay verbatim: never rewrite a quote to fit
the style.

ASD-STE100 Simplified Technical English is the controlled language of aircraft maintenance manuals. Apply its
writing rules. Do not apply its dictionary strictly.

## Apply (the 80%)

- **Short sentences.** At most 25 words in a sentence, at most 20 when it gives an order. One idea in each sentence.
  Split a sentence where "and", "which", "while" or a semicolon joins two ideas.
- **Active voice, simple tenses.** Simple present for what a method does ("The encoder makes features"), simple past
  for what the authors did ("The authors tuned the weights on the test split"). Not "is matching", "has been shown",
  "would allow".
- **No -ing verb forms.** Not "by comparing features" or ", making the match easier". Write "It compares the
  features." as its own sentence. An -ing word that is a name stays: training, heading, feature matching.
- **Keep the articles.** "The model predicts the depth of each pixel", not "Model predicts per-pixel depth".
- **At most three nouns in a row.** "The error of the position along the road", not "along-road position error metric".
- **One word, one meaning.** Choose one term for each thing and keep it across the whole review: "heading" for the
  camera's direction, never also "orientation" or "yaw". Use the term the glossary defines.
- **Conditions first.** "If the heading is not known, the error increases."
- **Plain, literal words.** No idioms, phrasal verbs or figures of speech ("rule out", "boils down to", "on par").
  No "e.g.", "i.e.", "etc.", "via", "utilize", "leverage". No contractions. Write "approximately", not "~".
- **Numbers keep their units** ("2.51 m", "0.28 degrees", "58.93%").

## Relax (the 20%)

- **Words.** Prefer short common words (use, find, give, show, make, compare, increase, decrease), but do not check
  the STE dictionary. When the simple word is clumsy or less exact, use the normal technical word.
- **Technical names and technical verbs are allowed.** Names: DINOv2, KITTI Test-2, BEV, homography. Verbs of the
  field: train, fine-tune, encode, decode, render, warp, regress, sample. Define each technical name the first time it
  appears: in `glossary` on a slide, in the same sentence in the synthesis.
- **Passive voice is allowed** when the actor is unknown or does not matter ("No weights were found").
- **Names stay as the papers write them**: methods, datasets, metrics and their split labels.

## Examples

| Before | After |
|---|---|
| Lifts dashcam pixels into 3D blobs, renders top-down, matches satellite | The method puts dashcam pixels in 3D and renders a top-down map |
| Unlike P170, it does not assume flat ground. Unlike P263, it renders a dense top-down map instead of scoring sparse 3D points per pose guess. | G2SWeakly (P170) assumes flat ground, but BevSplat does not. CROSS (P263) scores a small set of 3D points for each possible pose. BevSplat renders a full top-down map. |
| Every design choice was tuned on panorama validation data with known heading, and no table reports spread across training runs, so small margins on driving images cannot be judged. | The authors tuned all design choices on panorama data with a known heading. No table shows the spread across training runs. Thus small differences on car-camera images can be random. |
| Teacher pseudo-label distillation | Student models trained on depth labels from a large teacher model |

## Checks

`merge-clusters`, `merge-explainers` and `merge-synthesis` list each text that breaks a rule a script can check
(sentence length, avoided words, -ing verb forms, contractions, cluster names outside 6-12 words) under `style` in
their report. These are notes, not errors: the target is 80%, and a note can be a false alarm.
