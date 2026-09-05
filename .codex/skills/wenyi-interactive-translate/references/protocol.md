# Interactive response protocol

The driver emits one current work item. Copy its `batch_id` exactly into the response.

## Body response

```json
{
  "kind": "body",
  "batch_id": "body:...",
  "chapter": 0,
  "segment_indices": [0, 1],
  "translations": ["第一段译文。", "第二段译文。"],
  "terms": [
    {
      "source": "Case",
      "target": "凯斯",
      "type": "人物",
      "aliases": [],
      "gender": "男",
      "note": "主人公"
    }
  ]
}
```

Required fields are `kind`, `batch_id`, `chapter`, `segment_indices`, and
`translations`. `terms` is optional and defaults to an empty list. Allowed term fields are
`source`, `target`, `type`, `aliases`, `gender`, `reading`, and `note`.

The translation array must have exactly the same length and order as `segment_indices`.
Every value must be a non-empty string. Keep a symbolic source segment unchanged.

## Title response

```json
{
  "kind": "titles",
  "batch_id": "titles:...",
  "sources": ["PART ONE", "CHIBA CITY BLUES"],
  "translations": ["第一部", "千叶城蓝调"]
}
```

The source and translation arrays must exactly match the current title work item. Repeated
source titles are grouped by the driver and receive one consistent translation.

## Profile

```json
{
  "style_guide": "冷峻、凝练，保留赛博朋克技术感和原文意象。",
  "book_synopsis": "可留空；只写从样本或用户提供信息中可靠得出的概览。",
  "decisions": ["人物姓名采用常见音译", "不额外解释技术名词"]
}
```

Do not fabricate a whole-book synopsis from a short sample. An empty synopsis is valid.
