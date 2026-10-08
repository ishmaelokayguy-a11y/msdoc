# Task: floating pictures (`src/msdoc/images.py`, OfficeArt part)

Read CLAUDE.md first. You own the FLOATING part of `src/msdoc/images.py`
(functions with `floating`/`officeart`/`blip` in their name) and
`tests/test_images_floating.py`. Another agent writes inline pictures in the
same file in parallel: separate functions, keep `extract_images` as the
dispatcher, add rather than reorganise. A shared BLIP decoder (`decode_blip`)
is needed by both — if it does not exist yet when you start, write it in its
own function with tests; the other agent will reuse it.

Implement per the docstring in images.py: 0x08 anchors -> PlcSpaMom/PlcSpaHdr
-> shape id -> OfficeArtDgContainer shape -> pib -> BStore FBSE -> BLIP (inline
in BStore or at foDelay in WordDocument). Groups, blip fills. Reference:
msdffimp.cxx (SvxMSDffManager::Init, GetShape, GetBLIP, ProcessObj),
ww8graf.cxx (ReadGrafLayer1, Read_GrafLayer).

Images in headers come from the header drawing (PlcSpaHdr) and belong to story
"headers". Text in text-box shapes is NOT yours (stories.py handles textboxes
through PlcftxbxTxt) — only pictures.

Loop and "done" criteria as in prompts/14-inline-images.md, for floating images.
