"""One-off: OmniParser's icon caption weights (old remote-code Florence-2 layout) → native transformers Florence2.

The old layout needs trust_remote_code, i.e. running Python downloaded from the hub. The native
model in transformers 5 has the same network under other names, so only the names change here.
Run once in .vision-venv:  python vision/convert_caption.py
"""
import re
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file
from transformers import Florence2Config, Florence2ForConditionalGeneration, GenerationConfig

MODELS = Path(__file__).resolve().parent / "models"
OLD = MODELS / "icon_caption" / "model.safetensors"
OUT = MODELS / "icon_caption_hf"

RULES = [
    (r"^language_model\.lm_head\.", "lm_head."),
    (r"^language_model\.model\.", "model.language_model."),
    (r"^vision_tower\.", "model.vision_tower."),
    (r"^image_projection$", "model.multi_modal_projector.image_projection.weight"),
    (r"^image_proj_norm\.", "model.multi_modal_projector.image_proj_norm."),
    (r"^image_pos_embed\.", "model.multi_modal_projector.image_position_embed."),
    (r"^visual_temporal_embed\.", "model.multi_modal_projector.visual_temporal_embed."),
]
# Inside DaViT blocks the old code wrapped layers in PreNorm(fn=...) and named convs differently
BLOCK_RULES = [
    (r"\.(channel_attn|window_attn)\.norm\.", ".norm1."),
    (r"\.ffn\.norm\.", ".norm2."),
    (r"\.(channel_attn|window_attn)\.fn\.", r".\1."),
    (r"\.(conv[12])\.fn\.dw\.", r".\1."),
    (r"\.ffn\.fn\.net\.", ".ffn."),
    (r"^(model\.vision_tower\.convs\.\d+)\.proj\.", r"\1.conv."),
]


def rename(key):
    for pattern, new in RULES:
        if re.search(pattern, key):
            key = re.sub(pattern, new, key)
            break
    if key.startswith("model.vision_tower."):
        for pattern, new in BLOCK_RULES:
            key = re.sub(pattern, new, key)
    return key


def main():
    config = Florence2Config.from_pretrained(MODELS / "florence_native")
    old = load_file(OLD)
    # The native processor adds tokens (the image placeholder) past the old vocabulary, so the
    # tables are padded to the native size and generation is told never to emit the new rows
    real_vocab = old["language_model.lm_head.weight"].shape[0]
    with torch.device("meta"):
        model = Florence2ForConditionalGeneration(config)
    wanted = model.state_dict()
    new = {}
    for key, tensor in old.items():
        if key == "language_model.final_logits_bias":
            continue  # all zeros in BART-style heads; the native model has no such buffer
        name = rename(key)
        if key == "image_projection":
            tensor = tensor.T.contiguous()  # a raw (in, out) matrix before, an nn.Linear (out, in) now
        if name in ("lm_head.weight", "model.language_model.shared.weight"):
            pad = wanted[name].shape[0] - tensor.shape[0]
            tensor = torch.cat([tensor, torch.zeros(pad, tensor.shape[1], dtype=tensor.dtype)])
        new[name] = tensor
    missing = sorted(set(wanted) - set(new))
    extra = sorted(set(new) - set(wanted))
    wrong = sorted(k for k in set(new) & set(wanted) if tuple(new[k].shape) != tuple(wanted[k].shape))
    # Encoder and decoder token embeddings are tied to the shared table
    tied = [k for k in missing if k.endswith("embed_tokens.weight")]
    for k in tied:
        new[k] = new["model.language_model.shared.weight"]
    missing = [k for k in missing if k not in tied]
    print("missing:", missing[:10], len(missing))
    print("extra:", extra[:10], len(extra))
    print("shape mismatch:", wrong[:10], len(wrong))
    if missing or extra or wrong:
        sys.exit(1)
    OUT.mkdir(exist_ok=True)
    config.save_pretrained(OUT)
    generation = GenerationConfig.from_pretrained(MODELS / "icon_caption")  # the ids OmniParser was tuned with
    generation.suppress_tokens = list(range(real_vocab, config.text_config.vocab_size))
    generation.save_pretrained(OUT)
    # Encoder and decoder token tables are tied to the shared one when loading. transformers warns that
    # lm_head differs from it and keeps the two apart, which is right for this checkpoint
    save_file({k: v.contiguous() for k, v in new.items() if not k.endswith("embed_tokens.weight")},
              OUT / "model.safetensors", metadata={"format": "pt"})
    print("saved", OUT)


if __name__ == "__main__":
    main()
