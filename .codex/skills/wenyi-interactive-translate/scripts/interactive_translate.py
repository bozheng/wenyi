#!/usr/bin/env python3
"""Deterministic bridge between Codex turns and Wenyi's persistent book state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trans_novel.assemble.writer import assemble as assemble_book  # noqa: E402
from trans_novel.config import Config  # noqa: E402
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm  # noqa: E402
from trans_novel.ingest.models import Chapter, Segment  # noqa: E402
from trans_novel.ingest.segmenter import batch_segments, load_document  # noqa: E402
from trans_novel.pipeline.annotations import AnnotationService  # noqa: E402
from trans_novel.pipeline.context import RollingContext  # noqa: E402
from trans_novel.pipeline.runstore import (  # noqa: E402
    STATUS_DONE,
    RunStore,
    slugify,
    source_sha256,
)
from trans_novel.postprocess.punct import normalize_zh_segments  # noqa: E402

META_NAME = "interactive.json"
PROFILE_NAME = "interactive-profile.json"
SCHEMA_VERSION = 1


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def load_session(run_dir: str) -> tuple[RunStore, dict[str, Any]]:
    root = Path(run_dir).expanduser().resolve()
    store = RunStore(str(root), create=False)
    meta_path = root / META_NAME
    if not store.exists() or not meta_path.is_file():
        raise ValueError(f"not an interactive Wenyi run: {root}")
    meta = read_object(meta_path)
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported interactive state schema")
    source_path = str(meta.get("source_path") or "")
    if not source_path or not os.path.isfile(source_path):
        raise ValueError(f"source file is unavailable: {source_path}")
    store.ensure_source_identity(source_path)
    return store, meta


def config_for(meta: dict[str, Any]) -> Config:
    config_path = str(meta.get("config_path") or REPO_ROOT / "config.yaml")
    return Config.load(config_path)


def profile_for(store: RunStore) -> dict[str, Any]:
    path = Path(store.run_dir) / PROFILE_NAME
    if path.is_file():
        return read_object(path)
    return {
        "style_guide": "忠实、自然、适合中文阅读；保留原作语气、视角和歧义。",
        "book_synopsis": "",
        "decisions": [],
    }


def _default_run_dir(config: Config, title: str) -> Path:
    state_root = Path(config.state_dir).expanduser()
    if not state_root.is_absolute():
        state_root = Path.cwd() / state_root
    return (state_root / slugify(title)).resolve()


def command_init(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise ValueError(f"input file does not exist: {input_path}")
    config_path = Path(args.config).expanduser().resolve()
    config = Config.load(str(config_path))
    source_lang = args.source_lang or config.source_lang
    if source_lang in {"", "auto", None}:
        raise ValueError("interactive mode requires --source-lang; Codex should infer or ask")
    target_lang = args.target_lang or config.target_lang
    max_chars_per_batch = args.max_chars_per_batch or config.segment.max_chars_per_batch
    if max_chars_per_batch <= 0:
        raise ValueError("--max-chars-per-batch must be positive")
    digest = source_sha256(str(input_path))

    if input_path.suffix.lower() == ".pdf":
        title = input_path.stem
        run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else _default_run_dir(config, title)
        store = RunStore(str(run_dir))
        with store.lock():
            if store.exists():
                store.ensure_source_identity(str(input_path), actual_sha256=digest)
                _ensure_existing_meta(store, input_path, config_path, source_lang, target_lang)
                emit(_init_result(store, resumed=True))
                return
            store.begin_initialization(digest)
            document = load_document(
                str(input_path), source_lang, target_lang,
                split_segments=config.segment.max_chars_per_segment,
                cache_dir=store.source_dir,
                source_hash=digest,
            )
            _initialize_store(
                store,
                document,
                input_path,
                config_path,
                config,
                digest,
                max_chars_per_batch=max_chars_per_batch,
            )
    else:
        document = load_document(
            str(input_path), source_lang, target_lang,
            split_segments=config.segment.max_chars_per_segment,
        )
        run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else _default_run_dir(config, document.title)
        store = RunStore(str(run_dir))
        with store.lock():
            if store.exists():
                store.ensure_source_identity(str(input_path), actual_sha256=digest)
                _ensure_existing_meta(store, input_path, config_path, source_lang, target_lang)
                emit(_init_result(store, resumed=True))
                return
            store.begin_initialization(digest)
            _initialize_store(
                store,
                document,
                input_path,
                config_path,
                config,
                digest,
                max_chars_per_batch=max_chars_per_batch,
            )
    emit(_init_result(store, resumed=False))


def _ensure_existing_meta(
    store: RunStore,
    source_path: Path,
    config_path: Path,
    source_lang: str,
    target_lang: str,
) -> None:
    path = Path(store.run_dir) / META_NAME
    if not path.is_file():
        raise ValueError(
            "the matching state was created by the API-driven pipeline; choose a separate --run-dir"
        )
    meta = read_object(path)
    expected = (source_lang, target_lang)
    actual = (meta.get("source_lang"), meta.get("target_lang"))
    if actual != expected:
        raise ValueError(f"language mismatch: state={actual}, requested={expected}")
    if Path(str(meta.get("source_path"))).resolve() != source_path:
        raise ValueError("interactive state belongs to a different source path")
    if Path(str(meta.get("config_path"))).resolve() != config_path:
        meta["config_path"] = str(config_path)
        atomic_json(path, meta)


def _initialize_store(
    store: RunStore,
    document,
    input_path: Path,
    config_path: Path,
    config: Config,
    digest: str,
    *,
    max_chars_per_batch: int,
) -> None:
    manifest = store.stage_document(document, source_hash=digest)
    GlossaryStore(store.glossary_path).close()
    store.save_analysis({"style_guide": "", "interactive": True})
    store.save_context(RollingContext(max_recent_keep=40).to_dict())
    manifest["initialized"] = True
    manifest["interactive"] = True
    store.save_manifest(manifest)
    atomic_json(
        Path(store.run_dir) / META_NAME,
        {
            "schema_version": SCHEMA_VERSION,
            "source_path": str(input_path),
            "source_sha256": digest,
            "source_lang": document.source_lang,
            "target_lang": document.target_lang,
            "config_path": str(config_path),
            "max_chars_per_batch": max_chars_per_batch,
            "context_segments": config.pipeline.rolling_context_segments,
        },
    )
    store.finish_initialization()
    store.log_event("interactive_initialized", source_path=str(input_path))


def _init_result(store: RunStore, *, resumed: bool) -> dict[str, Any]:
    status = status_data(store)
    return {
        "ok": True,
        "resumed": resumed,
        "run_dir": str(Path(store.run_dir).resolve()),
        "needs_profile": not (Path(store.run_dir) / PROFILE_NAME).is_file(),
        "status": status,
    }


def command_sample(args: argparse.Namespace) -> None:
    store, meta = load_session(args.run_dir)
    segments: list[dict[str, Any]] = []
    manifest = store.load_manifest()
    for row in manifest.get("chapters", []):
        chapter = store.load_chapter(row["index"])
        for segment in chapter.text_segments:
            segments.append({
                "chapter": chapter.index,
                "chapter_title": chapter.title,
                "index": segment.index,
                "kind": segment.kind,
                "source": segment.source,
            })
    if not segments:
        selected: list[dict[str, Any]] = []
    else:
        points = [0, 1, len(segments) // 2, max(0, len(segments) - 2), len(segments) - 1]
        selected = [segments[index] for index in dict.fromkeys(points) if 0 <= index < len(segments)]
    emit({
        "kind": "sample",
        "source_lang": meta["source_lang"],
        "target_lang": meta["target_lang"],
        "segments": selected,
    })


def command_set_profile(args: argparse.Namespace) -> None:
    store, _meta = load_session(args.run_dir)
    profile = read_object(Path(args.file).expanduser().resolve())
    style = profile.get("style_guide")
    if not isinstance(style, str) or not style.strip():
        raise ValueError("profile.style_guide must be a non-empty string")
    synopsis = profile.get("book_synopsis", "")
    decisions = profile.get("decisions", [])
    if not isinstance(synopsis, str) or not isinstance(decisions, list) or not all(
        isinstance(item, str) for item in decisions
    ):
        raise ValueError("profile synopsis/decisions have invalid types")
    normalized = {
        "style_guide": style.strip(),
        "book_synopsis": synopsis.strip(),
        "decisions": [item.strip() for item in decisions if item.strip()],
    }
    atomic_json(Path(store.run_dir) / PROFILE_NAME, normalized)
    store.log_event("interactive_profile_saved")
    emit({"ok": True, "run_dir": store.run_dir, "profile": normalized})


def _grouped_batches(chapter: Chapter, max_chars: int) -> list[list[Segment]]:
    groups: list[list[Segment]] = []
    for raw in batch_segments(chapter.text_segments, max_chars):
        current: list[Segment] = []
        current_done: bool | None = None
        for segment in raw:
            done = bool(segment.target and segment.target.strip())
            if current and done != current_done:
                groups.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            groups.append(current)
    return groups


def _body_work(store: RunStore, meta: dict[str, Any]) -> dict[str, Any] | None:
    manifest = store.load_manifest()
    max_chars = int(meta.get("max_chars_per_batch") or 1800)
    for row in manifest.get("chapters", []):
        chapter = store.load_chapter(row["index"])
        for group in _grouped_batches(chapter, max_chars):
            if all(segment.target and segment.target.strip() for segment in group):
                continue
            indices = [segment.index for segment in group]
            sources = [segment.source for segment in group]
            batch_id = _batch_id("body", chapter.index, indices, sources)
            context = _context_before(store, chapter.index, indices[0], int(meta.get("context_segments") or 6))
            glossary = GlossaryStore(store.glossary_path)
            try:
                chapter_source = "\n".join(segment.source for segment in chapter.text_segments)
                terms = GlossaryStore.terms_in(glossary.all_terms(), chapter_source)
            finally:
                glossary.close()
            annotation_sets = AnnotationService.annotation_contexts_for_segments(
                chapter.text_segments,
                store.load_annotation_contexts(),
            )
            positions = {segment.index: pos for pos, segment in enumerate(chapter.text_segments)}
            return {
                "kind": "body",
                "batch_id": batch_id,
                "source_lang": meta["source_lang"],
                "target_lang": meta["target_lang"],
                "chapter": chapter.index,
                "chapter_title": chapter.title,
                "segment_indices": indices,
                "segments": [
                    {
                        "index": segment.index,
                        "kind": segment.kind,
                        "source": segment.source,
                        "continuation": segment.cont,
                        "annotations": annotation_sets[positions[segment.index]],
                    }
                    for segment in group
                ],
                "translated_context": context,
                "glossary": [term.__dict__ for term in terms],
                "profile": profile_for(store),
            }
    return None


def _title_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chapter in manifest.get("chapters", []):
        source = str(chapter.get("title") or "").strip()
        if source and not chapter.get("title_translated"):
            grouped.setdefault(source, []).append({"kind": "chapter", "index": chapter.get("index")})
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    for position, entry in enumerate(meta.get("toc_entries", []) or []):
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("title") or "").strip()
        if source and not entry.get("title_translated"):
            grouped.setdefault(source, []).append({"kind": "toc", "position": position})
    return [{"source": source, "locations": locations} for source, locations in grouped.items()]


def _title_work(store: RunStore) -> dict[str, Any] | None:
    records = _title_records(store.load_manifest())
    if not records:
        return None
    batch: list[dict[str, Any]] = []
    chars = 0
    for record in records:
        source = record["source"]
        if batch and (len(batch) >= 40 or chars + len(source) > 4000):
            break
        batch.append(record)
        chars += len(source)
    sources = [record["source"] for record in batch]
    glossary = GlossaryStore(store.glossary_path)
    try:
        terms = [term.__dict__ for term in glossary.all_terms()]
    finally:
        glossary.close()
    return {
        "kind": "titles",
        "batch_id": _batch_id("titles", -1, list(range(len(sources))), sources),
        "sources": sources,
        "glossary": terms,
        "profile": profile_for(store),
    }


def _batch_id(kind: str, chapter: int, indices: list[int], sources: list[str]) -> str:
    raw = json.dumps([kind, chapter, indices, sources], ensure_ascii=False, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _context_before(store: RunStore, chapter_index: int, segment_index: int, count: int) -> list[str]:
    values: list[str] = []
    manifest = store.load_manifest()
    for row in manifest.get("chapters", []):
        chapter = store.load_chapter(row["index"])
        for segment in chapter.text_segments:
            if chapter.index == chapter_index and segment.index == segment_index:
                return values[-count:] if count > 0 else []
            if segment.target and segment.target.strip():
                values.append(segment.target)
    return values[-count:] if count > 0 else []


def current_work(store: RunStore, meta: dict[str, Any]) -> dict[str, Any]:
    body = _body_work(store, meta)
    if body is not None:
        return body
    titles = _title_work(store)
    if titles is not None:
        return titles
    return {"kind": "complete", "run_dir": store.run_dir, "status": status_data(store)}


def command_next(args: argparse.Namespace) -> None:
    store, meta = load_session(args.run_dir)
    with store.lock():
        emit(current_work(store, meta))


def command_apply(args: argparse.Namespace) -> None:
    store, meta = load_session(args.run_dir)
    response = read_object(Path(args.file).expanduser().resolve())
    with store.lock():
        work = current_work(store, meta)
        if work.get("kind") == "complete":
            raise ValueError("translation is already complete")
        if response.get("kind") != work.get("kind") or response.get("batch_id") != work.get("batch_id"):
            raise ValueError("stale or mismatched batch; call next again")
        if work["kind"] == "body":
            _apply_body(store, work, response, meta)
        else:
            _apply_titles(store, work, response)
        emit({"ok": True, "applied": work["kind"], "status": status_data(store)})


def _apply_body(
    store: RunStore,
    work: dict[str, Any],
    response: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    expected_indices = work["segment_indices"]
    if response.get("chapter") != work["chapter"] or response.get("segment_indices") != expected_indices:
        raise ValueError("chapter or segment_indices do not match current batch")
    translations = response.get("translations")
    if not isinstance(translations, list) or len(translations) != len(expected_indices):
        raise ValueError("translations must match the current segment count")
    if any(not isinstance(value, str) or not value.strip() for value in translations):
        raise ValueError("every translation must be a non-empty string")

    parsed_terms = _parse_terms(response.get("terms", []))

    chapter = store.load_chapter(work["chapter"])
    by_index = {segment.index: segment for segment in chapter.text_segments}
    for index, target in zip(expected_indices, translations):
        segment = by_index.get(index)
        if segment is None or (segment.target and segment.target.strip()):
            raise ValueError(f"segment is missing or already translated: {index}")
        segment.target = target.strip()

    done = all(segment.target and segment.target.strip() for segment in chapter.text_segments)
    if done and str(meta.get("target_lang", "")).lower().startswith("zh"):
        normalized = normalize_zh_segments(
            [segment.target or "" for segment in chapter.text_segments],
            [segment.cont for segment in chapter.text_segments],
        )
        for segment, target in zip(chapter.text_segments, normalized):
            segment.target = target
    if done:
        store.save_chapter_with_status(chapter, STATUS_DONE)
    else:
        store.save_chapter(chapter)

    glossary = GlossaryStore(store.glossary_path)
    term_summary = {"inserted": 0, "unchanged": 0, "conflict": 0}
    try:
        for term in parsed_terms:
            result = glossary.upsert_term(term, chapter=chapter.index)
            term_summary[result] += 1
    finally:
        glossary.close()
    context = RollingContext(max_recent_keep=40)
    for row in store.load_manifest().get("chapters", []):
        current = store.load_chapter(row["index"])
        context.add_targets([segment.target or "" for segment in current.text_segments])
    store.save_context(context.to_dict())
    store.log_event(
        "interactive_batch_applied",
        batch_id=work["batch_id"], chapter=chapter.index,
        segment_indices=expected_indices, terms=term_summary,
    )


def _parse_terms(raw_terms: object) -> list[GlossaryTerm]:
    """Validate all proposed terms before any chapter or glossary mutation."""
    if not isinstance(raw_terms, list) or len(raw_terms) > 50:
        raise ValueError("terms must be a list with at most 50 entries")
    parsed: list[GlossaryTerm] = []
    for raw in raw_terms:
        if not isinstance(raw, dict):
            raise ValueError("every term must be an object")
        source = raw.get("source")
        target = raw.get("target")
        aliases = raw.get("aliases", [])
        if (
            not isinstance(source, str)
            or not source.strip()
            or not isinstance(target, str)
            or not target.strip()
        ):
            raise ValueError("term source and target must be non-empty strings")
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise ValueError("term aliases must be strings")
        parsed.append(
            GlossaryTerm(
                source=source.strip(),
                target=target.strip(),
                reading=str(raw.get("reading") or "").strip(),
                type=str(raw.get("type") or "术语").strip(),
                gender=str(raw.get("gender") or "").strip(),
                aliases=[item.strip() for item in aliases if item.strip()],
                note=str(raw.get("note") or "").strip(),
            )
        )
    return parsed


def _apply_titles(store: RunStore, work: dict[str, Any], response: dict[str, Any]) -> None:
    if response.get("sources") != work["sources"]:
        raise ValueError("title sources do not match current batch")
    translations = response.get("translations")
    if not isinstance(translations, list) or len(translations) != len(work["sources"]):
        raise ValueError("title translations must match current title count")
    if any(not isinstance(value, str) or not value.strip() for value in translations):
        raise ValueError("every title translation must be a non-empty string")
    mapping = dict(zip(work["sources"], [value.strip() for value in translations]))
    manifest = store.load_manifest()
    for chapter in manifest.get("chapters", []):
        source = str(chapter.get("title") or "").strip()
        if source in mapping and not chapter.get("title_translated"):
            chapter["title_translated"] = mapping[source]
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    for entry in meta.get("toc_entries", []) or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("title") or "").strip()
        if source in mapping and not entry.get("title_translated"):
            entry["title_translated"] = mapping[source]
    store.save_manifest(manifest)
    store.log_event("interactive_titles_applied", batch_id=work["batch_id"], count=len(mapping))


def status_data(store: RunStore) -> dict[str, Any]:
    manifest = store.load_manifest()
    total = translated = 0
    done_chapters = 0
    for row in manifest.get("chapters", []):
        chapter = store.load_chapter(row["index"])
        total += len(chapter.text_segments)
        translated += sum(bool(segment.target and segment.target.strip()) for segment in chapter.text_segments)
        done_chapters += row.get("status") == STATUS_DONE
    glossary = GlossaryStore(store.glossary_path)
    try:
        glossary_stats = glossary.stats()
    finally:
        glossary.close()
    return {
        "title": manifest.get("title"),
        "chapters_done": done_chapters,
        "chapters_total": len(manifest.get("chapters", [])),
        "segments_done": translated,
        "segments_total": total,
        "titles_remaining": len(_title_records(manifest)),
        "glossary": glossary_stats,
    }


def command_status(args: argparse.Namespace) -> None:
    store, _meta = load_session(args.run_dir)
    emit({"kind": "status", "run_dir": store.run_dir, "status": status_data(store)})


def command_assemble(args: argparse.Namespace) -> None:
    store, meta = load_session(args.run_dir)
    work = current_work(store, meta)
    if work.get("kind") != "complete" and not args.allow_partial:
        raise ValueError("translation is incomplete; use --allow-partial only for a preview")
    config = config_for(meta)
    source_path = str(meta["source_path"])
    out_format = args.format or ("docx" if Path(source_path).suffix.lower() == ".docx" else "epub")
    with store.assemble_lock():
        output = assemble_book(
            store, source_path,
            out_path=args.out,
            out_format=out_format,
            bilingual=args.bilingual,
            order=config.output.bilingual_order,
            preserve_source_style=config.output.bilingual_preserve_source_style,
            about_page=config.output.about_page,
            pdf_engine=args.pdf_engine,
        )
    emit({"ok": True, "output": str(Path(output).resolve()), "partial": work.get("kind") != "complete"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize or resume interactive state")
    init.add_argument("input")
    init.add_argument("--source-lang")
    init.add_argument("--target-lang")
    init.add_argument("--config", default="config.yaml")
    init.add_argument("--run-dir")
    init.add_argument("--max-chars-per-batch", type=int)
    init.set_defaults(func=command_init)
    for name, function in (("sample", command_sample), ("next", command_next), ("status", command_status)):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True)
        command.set_defaults(func=function)
    profile = commands.add_parser("set-profile")
    profile.add_argument("--run-dir", required=True)
    profile.add_argument("--file", required=True)
    profile.set_defaults(func=command_set_profile)
    apply = commands.add_parser("apply")
    apply.add_argument("--run-dir", required=True)
    apply.add_argument("--file", required=True)
    apply.set_defaults(func=command_apply)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--run-dir", required=True)
    assemble.add_argument("--format", choices=("epub", "txt", "html", "markdown", "pdf", "docx"))
    assemble.add_argument("--out")
    assemble.add_argument("--bilingual", action="store_true")
    assemble.add_argument("--allow-partial", action="store_true")
    assemble.add_argument("--pdf-engine", default="weasyprint", choices=("weasyprint", "fpdf2"))
    assemble.set_defaults(func=command_assemble)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
