"""Safetensors header parsing utilities."""

from __future__ import annotations

import json
import struct
from pathlib import Path


class SafetensorsHeaderError(ValueError):
    """Raised when a safetensors header cannot be read or parsed."""


def read_safetensors_header(path: Path, max_header_bytes: int = 8 * 1024 * 1024) -> dict:
    """
    Read the JSON header from a safetensors file without loading tensor data.

    File format:
    - 8 bytes: little-endian unsigned 64-bit header length
    - N bytes: JSON header
    - Remaining bytes: tensor data
    """
    with path.open("rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) != 8:
            raise SafetensorsHeaderError("File too short to contain a header length.")

        (header_len,) = struct.unpack("<Q", header_len_bytes)
        if header_len <= 0:
            raise SafetensorsHeaderError("Header length is invalid.")
        if header_len > max_header_bytes:
            raise SafetensorsHeaderError("Header is larger than the allowed limit.")

        header_bytes = f.read(header_len)
        if len(header_bytes) != header_len:
            raise SafetensorsHeaderError("Header appears truncated.")

    try:
        return json.loads(header_bytes.decode("utf-8"))
    except Exception as exc:
        raise SafetensorsHeaderError("Header JSON is invalid.") from exc


def classify_safetensors_header(header: dict, relpath: str | None = None) -> dict:
    """
    Classify a safetensors header using lightweight heuristics.
    Returns tags, confidence, and matched signals.
    """
    keys = [k for k in header.keys() if k != "__metadata__"]
    if not keys:
        return {"tags": [], "confidence": 0.0, "signals": []}
    relpath_text = (relpath or "").lower()

    def has_prefix(prefix: str) -> bool:
        return any(k.startswith(prefix) for k in keys)

    def has_substring(substr: str) -> bool:
        return any(substr in k for k in keys)

    has_unet = has_prefix("model.diffusion_model.")
    has_vae = has_prefix("first_stage_model.")
    has_cond = has_prefix("conditioner.")
    has_text0 = has_prefix("conditioner.embedders.0.")
    has_text1 = has_prefix("conditioner.embedders.1.")
    has_dual_text = has_text0 and has_text1
    has_cond_stage = has_prefix("cond_stage_model.")
    has_double_blocks = has_prefix("model.diffusion_model.double_blocks.") or has_prefix("double_blocks.")
    has_img_attn = any(".img_attn." in k for k in keys)
    has_txt_attn = any(".txt_attn." in k for k in keys)
    has_openclip = any(k.startswith("conditioner.embedders.0.model.") or k.startswith("conditioner.embedders.1.model.") for k in keys)
    has_clip_text = any(
        k.startswith("conditioner.embedders.0.transformer.text_model.")
        or k.startswith("conditioner.embedders.1.transformer.text_model.")
        or k.startswith("cond_stage_model.transformer.text_model.")
        for k in keys
    )
    has_main_image_encoder = any(k.startswith("conditioner.main_image_encoder.") for k in keys)
    has_main_text_encoder = any(k.startswith("conditioner.main_text_encoder.") for k in keys)
    has_transformer_blocks = any(k.startswith("model.diffusion_model.transformer_blocks.") for k in keys)
    has_transformer_blocks_root = any(k.startswith("transformer_blocks.") for k in keys)
    has_dit_blocks = any(k.startswith("pipe.dit.blocks.") or k.startswith("dit.blocks.") for k in keys)
    has_audio_blocks = any(".audio_" in k or "audio_to_video_attn" in k or "video_to_audio_attn" in k for k in keys)
    has_wan_blocks = any(
        k.startswith("model.diffusion_model.blocks.")
        or k.startswith("diffusion_model.blocks.")
        or k.startswith("blocks.")
        or k.startswith("pipe.dit.blocks.")
        or k.startswith("dit.blocks.")
        for k in keys
    )
    has_wan_attn = any(".cross_attn." in k for k in keys)
    has_wan_img_attn = any(".k_img." in k or ".v_img." in k or ".q_img." in k for k in keys)
    has_cap_embedder = any(k.startswith("cap_embedder.") for k in keys)
    has_context_refiner = any(k.startswith("context_refiner.") for k in keys)
    has_noise_refiner = any(k.startswith("noise_refiner.") for k in keys)
    has_adaln = any(".adaLN_modulation." in k for k in keys)
    has_x_embedder = any(k.startswith("x_embedder.") for k in keys)
    has_input_hint = any(k.startswith("input_hint_block.") for k in keys)
    has_zero_convs = any(k.startswith("zero_convs.") for k in keys)
    has_controlnet_prefix = any(k.startswith("controlnet.") for k in keys)
    has_controlnet_blocks = any(k.startswith("controlnet_blocks.") for k in keys)
    has_controlnet_single_blocks = any(k.startswith("controlnet_single_blocks.") for k in keys)
    has_controlnet_mode_embedder = any(k.startswith("controlnet_mode_embedder.") for k in keys)
    has_context_embedder = any(k.startswith("context_embedder.") for k in keys)
    has_add_embedding = any(k.startswith("add_embedding.") for k in keys)
    has_controlnet_cond_embedding = any(k.startswith("controlnet_cond_embedding.") for k in keys)
    has_lora_controlnet = "lora_controlnet" in header
    has_lora_keys = any(
        ".lora_down." in k
        or ".lora_up." in k
        or ".lora_A." in k
        or ".lora_B." in k
        or k.startswith("lora_")
        for k in keys
    )
    has_lora_unet = any(k.startswith("lora_unet_") for k in keys)
    has_lora_te = any(
        ("text_encoder" in k or "lora_te" in k or "cond_stage_model" in k or "conditioner.embedders" in k)
        and "lora" in k
        for k in keys
    )
    has_lora_te1 = any("lora_te1" in k for k in keys)
    has_lora_te2 = any("lora_te2" in k for k in keys)
    has_lllite_prefix = any(k.startswith("lllite_") for k in keys)
    has_t2i_adapter_prefix = any(k.startswith("adapter.body.") for k in keys)
    has_t2i_body_prefix = any(k.startswith("body.") for k in keys)
    has_t2i_blocks = any(".block1." in k or ".block2." in k for k in keys)
    has_t2i_resnets = any(".resnets." in k for k in keys)
    has_t2i_in_conv = any(".in_conv." in k for k in keys)
    has_flux_img_mlp = any(".img_mlp." in k for k in keys)
    has_flux_txt_mlp = any(".txt_mlp." in k for k in keys)
    has_flux_mod = any(".img_mod." in k or ".txt_mod." in k for k in keys)
    has_flux_add_proj = any(
        "add_k_proj" in k or "add_q_proj" in k or "add_v_proj" in k or "to_add_out" in k for k in keys
    )
    controlnet_base_dim = None
    controlnet_sdxl_hint = False
    for k in keys:
        if ".attn2.to_k.weight" in k:
            tensor = header.get(k)
            if isinstance(tensor, dict):
                shape = tensor.get("shape")
                if isinstance(shape, list) and len(shape) == 2:
                    controlnet_base_dim = shape[1]
                    break
    if controlnet_base_dim is None:
        add_key = "add_embedding.linear_1.weight"
        tensor = header.get(add_key)
        if isinstance(tensor, dict):
            shape = tensor.get("shape")
            if isinstance(shape, list) and len(shape) == 2:
                controlnet_base_dim = shape[1]

    if controlnet_base_dim and controlnet_base_dim >= 1280:
        controlnet_sdxl_hint = True

    signals = []
    if has_unet:
        signals.append("unet:model.diffusion_model")
    if has_vae:
        signals.append("vae:first_stage_model")
    if has_text0:
        signals.append("text:conditioner.embedders.0")
    if has_text1:
        signals.append("text:conditioner.embedders.1")

    tags: list[dict] = []
    signals_by_tag: dict[str, list[str]] = {}

    def add_tag(name: str, score: float, tag_signals: list[str]):
        if score <= 0:
            return
        tags.append({"name": name, "confidence": round(score, 3)})
        signals_by_tag[name] = tag_signals

    # SDXL detection (requires dual text encoders)
    sdxl_score = 0.0
    sdxl_signals = []
    if has_dual_text:
        sdxl_score = 0.65
        sdxl_signals.append("sdxl:dual_text")
    if has_dual_text and has_unet:
        sdxl_score = 0.78
        sdxl_signals.append("sdxl:unet")
    if has_dual_text and has_unet and has_vae:
        sdxl_score = 0.92
        sdxl_signals.append("sdxl:vae")

    # SD1/SD2 detection (cond_stage_model CLIP)
    sd12_score = 0.0
    sd12_signals = []
    if has_cond_stage:
        sd12_score = 0.6
        sd12_signals.append("sd1:cond_stage")
    if has_cond_stage and has_unet:
        sd12_score = 0.75
        sd12_signals.append("sd1:unet")
    if has_cond_stage and has_unet and has_vae:
        sd12_score = 0.88
        sd12_signals.append("sd1:vae")

    # Flux-like detection
    flux_score = 0.0
    flux_signals = []
    if has_double_blocks:
        flux_score = 0.7
        flux_signals.append("flux:double_blocks")
    if has_double_blocks and has_img_attn and has_txt_attn:
        flux_score = 0.9
        flux_signals.append("flux:img_txt_attn")

    # SDXL refiner detection (OpenCLIP-only text)
    sdxl_refiner_score = 0.0
    sdxl_refiner_signals = []
    if not has_dual_text and has_openclip:
        sdxl_refiner_score = 0.72
        sdxl_refiner_signals.append("sdxl-refiner:openclip")
        if not has_clip_text:
            sdxl_refiner_score = 0.82
            sdxl_refiner_signals.append("sdxl-refiner:no_clip_text")
        if has_unet:
            sdxl_refiner_score = min(0.92, sdxl_refiner_score + 0.08)
            sdxl_refiner_signals.append("sdxl-refiner:unet")
        if has_vae:
            sdxl_refiner_score = min(0.95, sdxl_refiner_score + 0.03)
            sdxl_refiner_signals.append("sdxl-refiner:vae")

    # Hunyuan3D detection
    hunyuan_score = 0.0
    hunyuan_signals = []
    if has_main_image_encoder or has_main_text_encoder:
        hunyuan_score = 0.85
        hunyuan_signals.append("hunyuan:main_encoder")
        if has_main_image_encoder and has_main_text_encoder:
            hunyuan_score = 0.9
            hunyuan_signals.append("hunyuan:dual_encoder")

    # LTX-2 / AV Transformer detection (audio+video transformer blocks)
    ltx_score = 0.0
    ltx_signals = []
    if has_transformer_blocks and has_audio_blocks:
        ltx_score = 0.82
        ltx_signals.append("ltx:transformer_blocks+audio")
    elif has_transformer_blocks:
        ltx_score = 0.7
        ltx_signals.append("ltx:transformer_blocks")

    # Z-Image Turbo (ZIT) detection
    zit_score = 0.0
    zit_signals = []
    if has_cap_embedder and has_context_refiner and has_adaln:
        zit_score = 0.8
        zit_signals.append("zit:cap_embedder+context_refiner+adaln")
        if has_noise_refiner:
            zit_score = 0.88
            zit_signals.append("zit:noise_refiner")
        if has_x_embedder:
            zit_score = min(0.92, zit_score + 0.04)
            zit_signals.append("zit:x_embedder")

    # Flux ControlNet detection
    controlnet_flux_score = 0.0
    controlnet_flux_signals = []
    if has_controlnet_blocks and has_controlnet_single_blocks and has_context_embedder:
        controlnet_flux_score = 0.9
        controlnet_flux_signals.append("controlnet-flux:blocks+context")
        if has_controlnet_mode_embedder:
            controlnet_flux_score = min(0.94, controlnet_flux_score + 0.04)
            controlnet_flux_signals.append("controlnet-flux:mode_embedder")

    # ControlNet detection
    controlnet_score = 0.0
    controlnet_signals = []
    controlnet_base_score = 0.0
    controlnet_base_signals = []
    if (
        has_input_hint
        or has_zero_convs
        or has_controlnet_prefix
        or has_controlnet_cond_embedding
        or has_add_embedding
        or has_lllite_prefix
        or controlnet_flux_score > 0
    ):
        controlnet_score = 0.88
        controlnet_signals.append("controlnet:input_hint/zero_convs")
        if has_controlnet_prefix:
            controlnet_score = min(0.95, controlnet_score + 0.05)
            controlnet_signals.append("controlnet:prefix")
        if has_controlnet_cond_embedding or has_add_embedding:
            controlnet_score = min(0.95, controlnet_score + 0.04)
            controlnet_signals.append("controlnet:cond_embedding")
        if has_lllite_prefix:
            controlnet_score = min(0.95, controlnet_score + 0.04)
            controlnet_signals.append("controlnet:lllite")
        if controlnet_flux_score > 0:
            controlnet_score = min(0.95, controlnet_score + 0.04)
            controlnet_signals.append("controlnet:flux_blocks")
        if controlnet_base_dim:
            if controlnet_base_dim == 768:
                controlnet_base_score = 0.9
                controlnet_base_signals.append("controlnet-base:sd1(768)")
            elif controlnet_base_dim == 1024:
                controlnet_base_score = 0.9
                controlnet_base_signals.append("controlnet-base:sd2(1024)")
            elif controlnet_base_dim >= 1280:
                controlnet_base_score = 0.85
                controlnet_base_signals.append("controlnet-base:sdxl(>=1280)")
        elif has_add_embedding or has_controlnet_cond_embedding:
            controlnet_base_score = max(controlnet_base_score, 0.84)
            controlnet_base_signals.append("controlnet-base:sdxl(add_embedding)")
        if has_lora_controlnet:
            controlnet_signals.append("controlnet:lora")

    # WAN 2.x detection
    wan_base_score = 0.0
    wan_base_signals = []
    if has_wan_blocks and has_wan_attn:
        wan_base_score = 0.78
        wan_base_signals.append("wan:blocks+cross_attn")

    wan22_score = 0.0
    wan22_signals = []
    wan21_score = 0.0
    wan21_signals = []

    # T2I-Adapter detection
    t2i_score = 0.0
    t2i_signals = []
    if has_t2i_adapter_prefix:
        t2i_score = 0.78
        t2i_signals.append("t2i:adapter.body")
    if has_t2i_body_prefix and has_t2i_blocks and (has_t2i_resnets or has_t2i_in_conv):
        t2i_score = max(t2i_score, 0.72)
        t2i_signals.append("t2i:body.blocks")

    # Slight boost if metadata explicitly mentions known families
    meta = header.get("__metadata__")
    if isinstance(meta, dict):
        meta_text = " ".join(str(v).lower() for v in meta.values())
        if "sdxl" in meta_text and sdxl_score > 0:
            sdxl_score = min(0.97, sdxl_score + 0.05)
            sdxl_signals.append("meta:sdxl")
        if "refiner" in meta_text and sdxl_refiner_score > 0:
            sdxl_refiner_score = min(0.97, sdxl_refiner_score + 0.05)
            sdxl_refiner_signals.append("meta:refiner")
        if ("sd15" in meta_text or "sd1.5" in meta_text or "sd 1.5" in meta_text) and sd12_score > 0:
            sd12_score = min(0.95, sd12_score + 0.05)
            sd12_signals.append("meta:sd15")
        if "flux" in meta_text and flux_score > 0:
            flux_score = min(0.95, flux_score + 0.05)
            flux_signals.append("meta:flux")
        if "hunyuan" in meta_text and hunyuan_score > 0:
            hunyuan_score = min(0.95, hunyuan_score + 0.05)
            hunyuan_signals.append("meta:hunyuan")
        if ltx_score > 0:
            if "ltx-2" in meta_text or "ltx2" in meta_text or "avtransformer3dmodel" in meta_text:
                ltx_score = min(0.96, ltx_score + 0.08)
                ltx_signals.append("meta:ltx2")
            if "causalvideoautoencoder" in meta_text:
                ltx_score = min(0.96, ltx_score + 0.04)
                ltx_signals.append("meta:causal_vae")
        if ("z image turbo" in meta_text or "z-image turbo" in meta_text or "zit" in meta_text) and zit_score > 0:
            zit_score = min(0.96, zit_score + 0.05)
            zit_signals.append("meta:zit")
        if "controlnet" in meta_text and controlnet_score > 0:
            controlnet_score = min(0.96, controlnet_score + 0.05)
            controlnet_signals.append("meta:controlnet")
        if "wan" in meta_text:
            if (
                "wan 2.2" in meta_text
                or "wan2.2" in meta_text
                or "wan2_2" in meta_text
                or "wan2-2" in meta_text
                or "wan22" in meta_text
                or "wanvideo22" in meta_text
            ):
                wan22_score = max(wan22_score, 0.9)
                wan22_signals.append("meta:wan2.2")
            if (
                "wan 2.1" in meta_text
                or "wan2.1" in meta_text
                or "wan2_1" in meta_text
                or "wan2-1" in meta_text
                or "wan21" in meta_text
            ):
                wan21_score = max(wan21_score, 0.88)
                wan21_signals.append("meta:wan2.1")

    add_tag("sdxl", sdxl_score, sdxl_signals)
    add_tag("sdxl-refiner", sdxl_refiner_score, sdxl_refiner_signals)
    # Filename/relpath hints for WAN versions
    if relpath_text:
        if "wan22" in relpath_text or "wan2_2" in relpath_text or "wan2.2" in relpath_text or "wan2-2" in relpath_text:
            wan22_score = max(wan22_score, 0.88)
            wan22_signals.append("path:wan2.2")
        if "wan21" in relpath_text or "wan2_1" in relpath_text or "wan2.1" in relpath_text or "wan2-1" in relpath_text:
            wan21_score = max(wan21_score, 0.85)
            wan21_signals.append("path:wan2.1")
        if "z_image_turbo" in relpath_text or "z-image-turbo" in relpath_text or "zimage_turbo" in relpath_text or "zit" in relpath_text:
            zit_score = max(zit_score, 0.86)
            zit_signals.append("path:zit")
        if "controlnet" in relpath_text:
            controlnet_score = max(controlnet_score, 0.9)
            controlnet_signals.append("path:controlnet")
        if "lllite" in relpath_text or "controllllite" in relpath_text or "controllite" in relpath_text:
            controlnet_score = max(controlnet_score, 0.9)
            controlnet_signals.append("path:lllite")
        if "t2i" in relpath_text or "t2i-adapter" in relpath_text or "t2i_adapter" in relpath_text:
            t2i_score = max(t2i_score, 0.85)
            t2i_signals.append("path:t2i")
        if "adapter" in relpath_text and t2i_score > 0:
            t2i_score = min(0.9, t2i_score + 0.04)
            t2i_signals.append("path:adapter")
        if "openpose" in relpath_text and t2i_score > 0:
            t2i_score = min(0.92, t2i_score + 0.04)
            t2i_signals.append("path:openpose")
        if "xl" in relpath_text and controlnet_score > 0:
            controlnet_base_score = max(controlnet_base_score, 0.82)
            controlnet_base_signals.append("path:xl")
            controlnet_sdxl_hint = True

    # Apply WAN base if version is known or leave generic
    if wan_base_score > 0:
        if wan22_score > 0:
            wan22_score = max(wan22_score, wan_base_score)
            wan22_signals.extend(wan_base_signals)
        if wan21_score > 0:
            wan21_score = max(wan21_score, wan_base_score)
            wan21_signals.extend(wan_base_signals)

    add_tag("sd1/2", sd12_score, sd12_signals)
    add_tag("flux", flux_score, flux_signals)
    add_tag("hunyuan3d", hunyuan_score, hunyuan_signals)
    add_tag("ltx-2", ltx_score, ltx_signals)
    add_tag("zit", zit_score, zit_signals)
    add_tag("controlnet", controlnet_score, controlnet_signals)
    add_tag("controlnet-lora", 0.85 if has_lora_controlnet else 0.0, ["controlnet:lora"])
    add_tag("controlnet-sd1", controlnet_base_score if controlnet_base_dim == 768 else 0.0, controlnet_base_signals)
    add_tag("controlnet-sd2", controlnet_base_score if controlnet_base_dim == 1024 else 0.0, controlnet_base_signals)
    add_tag("controlnet-sdxl", controlnet_base_score if controlnet_sdxl_hint else 0.0, controlnet_base_signals)
    add_tag("controlnet-flux", controlnet_flux_score, controlnet_flux_signals)
    add_tag("t2i-adapter", t2i_score, t2i_signals)
    add_tag("wan2.2", wan22_score, wan22_signals)
    add_tag("wan2.1", wan21_score, wan21_signals)

    # ------------------------------------------------------------------
    # LoRA heuristics (common for safetensors LoRA files)
    # ------------------------------------------------------------------
    if has_lora_keys:
        lora_signals = ["lora:keys"]
        lora_score = 0.9

        # Try to infer base model family from LoRA tensor shapes
        lora_ctx_dim = None
        lora_rank = None
        lora_dtype = None
        lora_channel_dim = None

        for k in keys:
            tensor = header.get(k)
            if not isinstance(tensor, dict):
                continue

            shape = tensor.get("shape")
            if not isinstance(shape, list) or len(shape) < 2:
                continue
            dtype = tensor.get("dtype")
            if dtype and not lora_dtype:
                lora_dtype = dtype

            if (
                "attn2" in k
                and ("to_k" in k or ".k." in k)
                and ("lora_down" in k or "lora_A" in k)
            ):
                lora_ctx_dim = shape[1]
                lora_rank = shape[0]
            if (
                "attn1" in k
                and ("to_k" in k or ".k." in k)
                and ("lora_down" in k or "lora_A" in k)
            ):
                lora_channel_dim = shape[1]
                lora_rank = lora_rank or shape[0]

        lora_base = None
        lora_base_score = 0.0
        lora_base_signals = []

        if lora_ctx_dim == 2048:
            lora_base = "lora-sdxl"
            lora_base_score = 0.88
            lora_base_signals.append("lora:ctx=2048")
        elif lora_ctx_dim == 1024:
            lora_base = "lora-sd2"
            lora_base_score = 0.85
            lora_base_signals.append("lora:ctx=1024")
        elif lora_ctx_dim == 768:
            lora_base = "lora-sd1"
            lora_base_score = 0.85
            lora_base_signals.append("lora:ctx=768")

        # WAN-style LoRA (video diffusion) detection
        if not lora_base and has_wan_blocks and has_wan_attn:
            # Check for large hidden dim ~5120
            for k in keys:
                tensor = header.get(k)
                if not isinstance(tensor, dict):
                    continue
                shape = tensor.get("shape")
                if isinstance(shape, list) and len(shape) >= 2 and shape[1] >= 4096:
                    lora_base = "lora-wan"
                    lora_base_score = 0.9
                    lora_base_signals.append("lora:wan_hidden>=4096")
                    if has_wan_img_attn:
                        lora_base = "lora-wan-i2v"
                        lora_base_score = 0.92
                        lora_base_signals.append("lora:wan_img_attn")
                    break

        # Dual TE LoRA is a strong SDXL signal
        if not lora_base and has_lora_te1 and has_lora_te2:
            lora_base = "lora-sdxl"
            lora_base_score = max(lora_base_score, 0.86)
            lora_base_signals.append("lora:te1+te2")

        # Flux-like LoRA (dual-stream DiT: img/txt MLPs with add_* projections)
        if (
            not lora_base
            and has_transformer_blocks_root
            and (has_flux_img_mlp or has_flux_txt_mlp or has_flux_mod)
            and has_flux_add_proj
        ):
            lora_base = "lora-flux"
            lora_base_score = max(lora_base_score, 0.84)
            lora_base_signals.append("lora:flux_transformer")

        # UNet-only LoRA without cross-attn context (ambiguous SD1/SD2 family)
        if not lora_base and has_lora_unet and lora_channel_dim:
            lora_base = "lora-sd1/2"
            lora_base_score = max(lora_base_score, 0.72)
            lora_base_signals.append("lora:unet_only")

        # Metadata hints for LoRA base
        meta = header.get("__metadata__")
        if isinstance(meta, dict):
            meta_text = " ".join(str(v).lower() for v in meta.values())
            if "sdxl" in meta_text:
                lora_base = lora_base or "lora-sdxl"
                lora_base_score = max(lora_base_score, 0.84)
                lora_base_signals.append("meta:sdxl")
            if "sd15" in meta_text or "sd1.5" in meta_text or "sd 1.5" in meta_text:
                lora_base = lora_base or "lora-sd1"
                lora_base_score = max(lora_base_score, 0.82)
                lora_base_signals.append("meta:sd1.5")
            if "sd2" in meta_text or "sd 2" in meta_text:
                lora_base = lora_base or "lora-sd2"
                lora_base_score = max(lora_base_score, 0.8)
                lora_base_signals.append("meta:sd2")
            if "wan" in meta_text:
                lora_base = lora_base or "lora-wan"
                lora_base_score = max(lora_base_score, 0.86)
                lora_base_signals.append("meta:wan")
            if "flux" in meta_text:
                lora_base = "lora-flux"
                lora_base_score = max(lora_base_score, 0.9)
                lora_base_signals.append("meta:flux")
            if "zimage" in meta_text or "z-image" in meta_text or "z image" in meta_text or "zit" in meta_text:
                lora_base = "lora-zit"
                lora_base_score = max(lora_base_score, 0.88)
                lora_base_signals.append("meta:zimage")

        # Add helpful signals to lora tag
        if lora_rank:
            lora_signals.append(f"lora:rank={lora_rank}")
        if lora_channel_dim:
            lora_signals.append(f"lora:channel_dim={lora_channel_dim}")
        if lora_ctx_dim:
            lora_signals.append(f"lora:ctx_dim={lora_ctx_dim}")
        if lora_dtype:
            lora_signals.append(f"lora:dtype={lora_dtype}")

        # Append tags
        add_tag("lora", lora_score, lora_signals)
        if has_lora_te:
            add_tag("lora-te", 0.82, ["lora:text_encoder"])
        if lora_base:
            add_tag(lora_base, lora_base_score, lora_base_signals)

    tags.sort(key=lambda t: t["confidence"], reverse=True)
    confidence = tags[0]["confidence"] if tags else 0.0

    # Preserve a flat list of signals for quick debugging
    signals = []
    for sigs in signals_by_tag.values():
        signals.extend(sigs)

    return {
        "tags": tags,
        "confidence": confidence,
        "signals": signals,
        "signals_by_tag": signals_by_tag,
    }
