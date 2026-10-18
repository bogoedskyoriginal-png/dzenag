"""Real-photo acquisition, visual review and deterministic article assembly; no image generation."""
from __future__ import annotations

import base64
import hashlib
import html
import io
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from dzen_agent.editorial_contracts import ImageV1, WriterV1
from dzen_agent.editorial_pipeline import approved_concept, run_context, validate_run
from dzen_agent.errors import UserActionRequired
from dzen_agent.store import dumps, now_iso

MAX_BYTES = 15 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Разрешены только публичные HTTP(S) URL без паролей.")
    for entry in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)):
        if not ipaddress.ip_address(entry[4][0]).is_global:
            raise ValueError("Скачивание из локальной сети запрещено.")


def fetch_public(url, limit=MAX_BYTES):
    # A separate client deliberately never receives AITUNNEL Authorization headers.
    with httpx.Client(timeout=25, follow_redirects=False, headers={"User-Agent": "DzenAgent/1.0"}) as client:
        for _ in range(5):
            public_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError("Слишком большой файл изображения или страницы.")
                return bytes(raw)
    raise ValueError("Слишком много перенаправлений источника.")


def download_asset(asset, folder: Path):
    if asset.rights_status == "restricted" or not asset.source_image_url:
        raise ValueError("Источник ограничен либо не содержит прямого URL изображения.")
    page = html.unescape(fetch_public(str(asset.source_page_url), 2 * 1024 * 1024).decode("utf-8", errors="replace"))
    image_url = str(asset.source_image_url)
    links = [urljoin(str(asset.source_page_url), match) for match in re.findall(r'''["']([^"'<>\s]+)["']''', page)]
    if image_url not in links and image_url not in page.replace("\\/", "/"):
        raise ValueError("Прямой URL изображения не найден на странице-источнике.")
    raw = fetch_public(image_url)
    with Image.open(io.BytesIO(raw)) as picture:
        if picture.width * picture.height > 40_000_000 or min(picture.size) < 200:
            raise ValueError("Размер изображения непригоден.")
        picture.load()
        picture = ImageOps.exif_transpose(picture).convert("RGB")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (digest(raw)[:24] + ".png")
        picture.save(path, "PNG")
        asset.local_file_ref = str(path.resolve())
        asset.file_hash = digest(path.read_bytes())
        asset.width, asset.height = picture.size
        asset.download_status = "downloaded"
        # Downloading the file is not visual identification; that happens in a separate vision call.
        asset.authenticity_status = "uncertain"
        asset.authenticity_basis = "Файл скачан со страницы-источника; визуальная проверка ещё не выполнена."
    return asset


def make_cover(source: Path, text: str, target: Path):
    if not text.strip() or len(text) > 140:
        raise ValueError("Для обложки нужен короткий текст до 140 символов.")
    canvas = Image.new("RGB", (1600, 900), "#11151c")
    with Image.open(source) as picture:
        # Contain preserves every part of the real photo, including watermarks and faces.
        photo = ImageOps.contain(ImageOps.exif_transpose(picture).convert("RGB"), (1600, 670))
        canvas.paste(photo, ((1600 - photo.width) // 2, (670 - photo.height) // 2))
    draw = ImageDraw.Draw(canvas)
    font_path = next((p for p in (Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")) if p.is_file()), None)
    if font_path is None:
        raise ValueError("Не найден шрифт с поддержкой кириллицы.")
    lines = []
    for size in range(78, 27, -2):
        font = ImageFont.truetype(str(font_path), size)
        lines, current = [], ""
        for word in text.split():
            trial = (current + " " + word).strip()
            if draw.textbbox((0, 0), trial, font=font)[2] > 1480:
                if not current:
                    raise ValueError("Слишком длинное слово для обложки.")
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        if len(lines) * (size + 12) <= 190:
            break
    else:
        raise ValueError("Текст не помещается на обложке.")
    y = 695 + (190 - len(lines) * (size + 12)) // 2
    for line in lines:
        draw.text((60, y), line, font=font, fill="white", stroke_width=1)
        y += size + 12
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG")
    return target


def assemble(body, headline, placements, assets, cover_path, cover_in_body=True):
    lookup = {a.asset_id: a for a in assets}
    insertions, seen, sections = [], set(), set()
    for place in placements:
        if place.role == "cover":
            continue  # The actual generated cover is inserted once below by Python.
        asset = lookup.get(place.asset_id)
        if not asset or asset.authenticity_status != "verified" or asset.download_status != "downloaded":
            raise ValueError("Размещение требует проверенного скачанного изображения.")
        if asset.file_hash in seen or place.target_section_heading in sections:
            raise ValueError("Повтор изображения или несколько изображений в одном разделе.")
        if place.visible_caption is not None:
            raise ValueError("Видимые подписи отключены.")
        heading = re.search(r"(?m)^#{1,6}\s+" + re.escape(place.target_section_heading) + r"\s*$", body)
        if not heading:
            raise ValueError("Раздел для иллюстрации не найден в тексте Writer.")
        next_heading = re.search(r"(?m)^#{1,6}\s+", body[heading.end():])
        end = heading.end() + next_heading.start() if next_heading else len(body)
        anchor = place.anchor_text
        matches = list(re.finditer(re.escape(anchor), body[heading.end():end])) if anchor else []
        if not matches or place.occurrence_index >= len(matches):
            raise ValueError("Точное место вставки не найдено в выбранном разделе.")
        position = heading.end() + matches[place.occurrence_index].end()
        # Insert after the complete paragraph/cast block, never into Writer's text.
        paragraph_end = body.find("\n\n", position, end)
        position = paragraph_end + 2 if paragraph_end >= 0 else end
        if place.placement_rule == "after_cast_block":
            block = body[heading.end():position]
            if "Режиссер" not in block or "В главных ролях" not in block:
                raise ValueError("Блок режиссёра и актёров для вставки не найден.")
        elif place.placement_rule not in ("after_heading", "after_paragraph"):
            raise ValueError("Неподдерживаемое правило вставки изображения.")
        insertions.append((position, {"type": "image", "asset_id": asset.asset_id,
            "local_file_ref": asset.local_file_ref, "alt_text": place.alt_text, "visible_caption": None}))
        seen.add(asset.file_hash)
        sections.add(place.target_section_heading)
    blocks, cursor = [], 0
    if cover_in_body and cover_path:
        blocks.append({"type": "image", "role": "cover", "local_file_ref": str(cover_path), "visible_caption": None})
    for position, block in sorted(insertions, key=lambda x: x[0]):
        blocks.extend([{"type": "text", "markdown": body[cursor:position]}, block])
        cursor = position
    blocks.append({"type": "text", "markdown": body[cursor:]})
    assert "".join(b["markdown"] for b in blocks if b["type"] == "text") == body
    return {"headline": headline, "original_markdown": body, "original_content_hash": digest(body.encode()),
            "cover_file_ref": str(cover_path) if cover_path else None, "blocks": blocks,
            "sources": [a.model_dump(mode="json") for a in assets]}


def prepare_visuals(service, article_id, call_key, *, force=False):
    from dzen_agent.services import citation_urls
    article = service.store.get(article_id)
    detail = service.store.detail(article_id)
    row = service.store.one("SELECT * FROM concepts WHERE id=?", (detail.get("concept_id"),))
    if not row:
        raise UserActionRequired("Image требует утверждённый концепт и результат Writer.")
    concept = approved_concept(service, row)
    writer = WriterV1.model_validate(json.loads(detail["metadata_json"]).get("writer_contract"))
    if (writer.concept_id, writer.concept_revision) != (concept["concept_id"], concept["revision"]):
        raise UserActionRequired("Writer относится к другой версии концепта.")
    if writer.status != "ok" or not writer.handoff.ready_for_qa:
        raise UserActionRequired("Writer не готов к Image.")
    # Manual text edits are authoritative. Image may only add visual blocks.
    writer.content.headline, writer.content.article_markdown = article.title, article.body
    revision_hash = digest(dumps([article.title, article.body, concept["revision"]]).encode())[:16]
    call_key += ":" + revision_hash
    saved = service.store.one("SELECT payload_json FROM visual_results WHERE call_key=?", (call_key,))
    if saved and not force:
        saved_report = ImageV1.model_validate_json(saved["payload_json"])
        if saved_report.assembly.assembled_document_ref and saved_report.cover.status == "ready":
            active = service.store.active_image(article_id)
            if active and Path(active["path"]).is_file():
                return active["id"]
    model = service.settings.get("visual_model") or service.settings["research_model"]
    catalog = service.settings.get("public_catalog", {}).get("chat", {})
    if catalog and "image" not in catalog.get(model, {}).get("modalities", {}).get("input", []):
        raise UserActionRequired("Для Image выберите поисковую модель с поддержкой просмотра изображений.")
    folder = service.store.database_path.parent / "images" / str(article_id) / revision_hash
    folder.mkdir(parents=True, exist_ok=True)
    research_row = service.store.one("SELECT payload_json FROM research_jobs WHERE id=?", (detail["research_id"],))
    research = json.loads(research_row["payload_json"]).get("editorial_contract") or {}
    context = {"run_context": run_context(service), "concept": concept,
        "writer": writer.model_dump(mode="json"), "research": research,
        "visual_config": {"cover_width": 1600, "cover_height": 900, "cover_aspect_ratio": "16:9",
            "preferred_image_sources": ["Кинориум", "официальные страницы и пресс-киты"],
            "allowed_image_operations": ["resize", "letterbox", "text_overlay"],
            "inline_image_policy": "one_per_section_after_cast_block_no_captions"},
        "publishing_config": {"target_platform": "dzen", "upload_mode": "draft", "publication_permission": False},
        "tool_capabilities": {"web_image_search": True, "image_inspection": False, "image_download": False,
            "image_editing": False, "image_upload": False, "article_assembly": False, "dzen_draft_upload": False},
        "execution_phase": "search_candidates_only; files and inspection will be supplied by Python afterwards"}
    report, response = service.structured("IMAGE", ImageV1, context, call_key + ":search", article_id=article_id,
                                          operation="IMAGE_SEARCH")
    validate_run(report, service)
    if (report.concept_id, report.concept_revision) != (concept["concept_id"], concept["revision"]):
        raise ValueError("Image изменил концепт.")
    available, inputs = [], []
    urls = citation_urls(response)
    if len(report.assets) > 12:
        report.issues.append("За один проход проверяется не более 12 фотографий.")
        report.assets = report.assets[:12]
    for asset in report.assets:
        if service.store.settings()["stop_requested"]:
            raise UserActionRequired("Агент остановлен перед скачиванием изображения.")
        asset.local_file_ref = asset.file_hash = None
        asset.download_status = "not_attempted"
        asset.authenticity_status = "uncertain"
        try:
            if str(asset.source_page_url).rstrip("/") not in urls:
                raise ValueError("Страница изображения не подтверждена поисковым ответом.")
            download_asset(asset, folder)
            with Image.open(asset.local_file_ref) as picture:
                picture.thumbnail((1024, 1024))
                buffer = io.BytesIO()
                picture.save(buffer, "JPEG", quality=85)
            inputs.extend([{"type": "text", "text": "asset_id=" + asset.asset_id},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}}])
            available.append(asset)
        except (ValueError, OSError, httpx.HTTPError) as error:
            asset.download_status = "failed"
            asset.issues.append(str(error))
    if available:
        context.update(execution_phase="inspect_actual_images_and_propose_cover_and_placements; Python executes file operations",
            tool_results={"assets": [a.model_dump(mode="json") for a in available]})
        context["tool_capabilities"]["image_inspection"] = True
        reviewed, _ = service.structured("IMAGE", ImageV1, context, call_key + ":inspect", article_id=article_id,
            operation="IMAGE_INSPECT", image_inputs=inputs)
        validate_run(reviewed, service)
        if (reviewed.concept_id, reviewed.concept_revision) != (concept["concept_id"], concept["revision"]):
            raise ValueError("Image inspection изменил концепт.")
        actual = {a.asset_id: a for a in available}
        for selected in reviewed.assets:
            original = actual.get(selected.asset_id)
            if not original or selected.source_page_url != original.source_page_url or selected.source_image_url != original.source_image_url:
                raise ValueError("Image inspection подменил скачанный источник.")
            if selected.related_work_id and selected.related_work_id not in {w["work_id"] for w in research.get("works", [])}:
                raise ValueError("Image ссылается на неизвестное произведение Research.")
            selected.local_file_ref, selected.file_hash = original.local_file_ref, original.file_hash
            selected.width, selected.height = original.width, original.height
            selected.download_status, selected.rights_status = "downloaded", original.rights_status
        selected_ids = {a.asset_id for a in reviewed.assets}
        failed = [a for a in report.assets if a.asset_id not in selected_ids]
        report = reviewed
        report.assets.extend(failed)
    # Execution statuses, hashes and paths are always set by actual operations below.
    report.publishing_handoff.execution_status = "not_attempted"
    report.publishing_handoff.verified_in_dzen = False
    report.publishing_handoff.draft_id = report.publishing_handoff.draft_url = None
    report.publishing_handoff.issues = ["Живой адаптер загрузки оформленной статьи в Дзен не подключён."]
    report.assembly.original_content_hash = digest(article.body.encode())
    report.assembly.assembled_document_ref = None
    report.assembly.headline = article.title
    report.assembly.visible_captions = False
    report.cover.width, report.cover.height, report.cover.aspect_ratio = 1600, 900, "16:9"
    report.cover.status, report.cover.cover_asset_id = "unavailable", None
    report.placements = [p for p in report.placements if p.role == "inline"]
    cover_path = None
    try:
        source = next((a for a in report.assets if a.asset_id == report.cover.source_asset_id
            and a.authenticity_status == "verified" and a.download_status == "downloaded"), None)
        if source:
            cover_path = make_cover(Path(source.local_file_ref), report.cover.cover_text, folder / "cover.png")
            cover_id = "LOCAL_COVER_" + digest(call_key.encode())[:10]
            report.cover.status, report.cover.cover_asset_id = "ready", cover_id
            report.assets.append(source.model_copy(update={"asset_id": cover_id, "media_type": "other",
                "local_file_ref": str(cover_path.resolve()), "file_hash": digest(cover_path.read_bytes()),
                "width": 1600, "height": 900,
                "authenticity_basis": "Локальная компоновка из " + source.asset_id + "; фото сохранено целиком, добавлена текстовая плашка."}))
        else:
            report.issues.append("Нет подлинного проверенного кадра для обложки.")
        document = assemble(article.body, article.title, report.placements, report.assets, cover_path,
                            report.assembly.cover_in_body)
        document_path = folder / "article.json"
        document_path.write_text(dumps(document), encoding="utf-8")
        report.assembly.assembled_document_ref = str(document_path.resolve())
        report.assembly.status = "ready" if cover_path else "partial"
        report.visual_plan.selected_inline_images = sum(b["type"] == "image" and b.get("role") != "cover"
                                                       for b in document["blocks"])
        report.status = "ok" if cover_path else "partial"
    except (ValueError, OSError) as error:
        report.status = "partial"
        report.assembly.status = "blocked"
        report.assembly.validation_issues.append(str(error))
    service.store.execute("INSERT INTO visual_results(article_id,call_key,payload_json,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(call_key) DO UPDATE SET payload_json=excluded.payload_json,created_at=excluded.created_at",
        (article_id, call_key, report.model_dump_json(), now_iso()))
    if not cover_path or report.assembly.status != "ready":
        raise UserActionRequired("Image: оформление требует проверки. Текст, источники и отчёт сохранены; "
                                 "повторные платные запросы автоматически не выполняются.")
    return service.attach_image(article_id, str(cover_path.resolve()), model=model,
        prompt_version=service.prompts["IMAGE"]["id"], origin="REAL_PHOTO")


def validate_visuals(store, article_id):
    article = store.get(article_id)
    row = store.one("SELECT payload_json FROM visual_results WHERE article_id=? ORDER BY id DESC LIMIT 1", (article_id,))
    if not row:
        return ["Нет результата визуальной сборки статьи."]
    report = ImageV1.model_validate_json(row["payload_json"])
    issues = []
    if report.assembly.status != "ready" or report.cover.status != "ready":
        issues.append("Визуальная сборка или обложка не готова.")
    if report.assembly.original_content_hash != digest(article.body.encode()):
        issues.append("Текст изменился после визуальной сборки. Повторите Image для актуального текста.")
    path = Path(report.assembly.assembled_document_ref) if report.assembly.assembled_document_ref else None
    if not path or not path.is_file():
        return issues + ["Нет файла собранного документа."]
    document = json.loads(path.read_text(encoding="utf-8"))
    text = "".join(b["markdown"] for b in document["blocks"] if b["type"] == "text")
    if text != article.body or document["original_markdown"] != article.body or document["headline"] != article.title:
        issues.append("После сборки потеряны или изменены фрагменты текста/заголовок Writer.")
    assets = {a.asset_id: a for a in report.assets}
    for block in document["blocks"]:
        if block["type"] != "image":
            continue
        image_path = Path(block["local_file_ref"])
        if not image_path.is_file():
            issues.append("Отсутствует файл иллюстрации.")
            continue
        if block.get("visible_caption") is not None:
            issues.append("Появилась видимая подпись под изображением.")
        if block.get("role") == "cover":
            with Image.open(image_path) as picture:
                if picture.width * 9 != picture.height * 16:
                    issues.append("Обложка имеет неверные пропорции.")
        else:
            asset = assets.get(block.get("asset_id"))
            if not asset or asset.authenticity_status != "verified" or asset.file_hash != digest(image_path.read_bytes()):
                issues.append("Иллюстрация не прошла проверку соответствия или файл изменён.")
    cover_path = Path(document["cover_file_ref"]) if document.get("cover_file_ref") else None
    active = store.active_image(article_id)
    if not active or not cover_path or Path(active["path"]).resolve() != cover_path.resolve():
        issues.append("Активная обложка не совпадает с собранным документом.")
    source = assets.get(report.cover.source_asset_id)
    if not source or source.authenticity_status != "verified" or not source.local_file_ref:
        issues.append("Не подтверждён исходный реальный кадр обложки.")
    cover_asset = assets.get(report.cover.cover_asset_id)
    if not cover_path or not cover_path.is_file() or not cover_asset or cover_asset.file_hash != digest(cover_path.read_bytes()):
        issues.append("Файл обложки изменён или не подтверждён результатом компоновки.")
    return issues
