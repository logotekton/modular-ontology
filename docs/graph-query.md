# Graph representation and structured queries

The Graph Explorer renders directed relationships. Arrowheads indicate source →
target; parallel predicates use separate curves, and explicit self-relations remain
visible. The inspector separates outgoing (→), incoming (←), and self (↺)
relationships. Identical source node IDs in different packs keep separate identities.

The browser displays up to 4,000 nodes by default, ordered by degree. Selected and
AI-highlighted nodes take priority over the ordinary display budget. Type, degree,
hop, and name/ID search filters operate on the loaded graph; they do not filter the entire source
pack on the server. The footer distinguishes displayed, loaded, and source totals.
Use structured queries for exhaustive filtering and aggregation, not canvas counts.

## Structured query syntax

These examples are MCP tool arguments, not SQL, Cypher, or text for the AI question
box. Discover packs and their property names with `mo_project_pack_list` and
`mo_pack_schema`; discover supported syntax through `mo_tool_manifest.queryLanguage`.

`where` accepts three equivalent field forms, combined with implicit AND:

```json
{"weight_kg": {"gte": 100}}
```

```json
{"weight_kg__gte": 100}
```

```json
{"module_id": "1-01-A"}
```

The last example is shorthand for `{"module_id": {"eq": "1-01-A"}}`.
Node properties can be addressed by their name or a `properties.` prefix; nested
properties use dotted paths. Projected keys keep the existing compact names unless
two requested paths would collide; collisions retain the requested qualified paths
(for example, `id` and `properties.id`) so neither value is lost.
Supported operators are `eq`, `ne`, `in`, `not_in`,
`contains`, `startswith`, `endswith`, `regex`, `wildcard`, `gt`, `gte`, `lt`, `lte`,
and `exists`. `contains`, `startswith`, and `endswith` ignore case; `regex` and
`wildcard` are case-sensitive. `exists` takes a boolean and distinguishes a missing
field from a field whose value is null.

Combine expressions with `$and` and `$or` arrays, or `$not` with one object:

```json
{
  "pack_id": "your-pack-id",
  "node_type": "Module",
  "where": {
    "$and": [
      {"weight_kg__gte": 100},
      {"$or": [{"floor": 2}, {"floor": 3}]},
      {"$not": {"status": "demolished"}}
    ]
  },
  "fields": ["id", "module_id"],
  "order_by": [{"field": "weight_kg", "direction": "desc"}],
  "limit": 20,
  "offset": 0
}
```

Pass that object to `mo_filtered_search_nodes`. Sorting happens before projection
and pagination: `weight_kg` need not appear in `fields`. `order_by` also accepts
strings (`"module_id"`, `"-weight_kg"`) and objects with `natural: true` or
`nulls: "first" | "last"`. Null and missing values sort last by default in either
direction. Natural sorting puts `M2` before `M10`.

Numeric comparisons accept finite numbers and whole numeric strings such as
`"1,200.5"` or `"1e3"`. Identifiers such as `"1-01-A"` and `"1-02-A"` stay distinct.
Unit-bearing text such as `"12 kg"` is not a numeric query value; use a normalized
numeric property. Numeric-looking codes that require textual comparison can use
an anchored `regex`. Invalid operators, regular expressions, logical shapes,
sort directions, and aggregation specifications raise explicit tool errors.

## Aggregation

Call `mo_aggregate_nodes`, for example:

```json
{
  "pack_id": "your-pack-id",
  "node_type": "Module",
  "where": {"weight_kg__exists": true},
  "group_by": ["floor"],
  "metrics": [
    {"agg": "count", "as": "modules"},
    {"agg": "sum", "field": "weight_kg", "as": "total_weight_kg"}
  ],
  "order_by": [{"field": "floor", "natural": true}]
}
```

Structured node queries read all available source nodes instead of using the visualization
sample. Basic node/edge browsing and context tools retain resource-limited samples;
their `scope.sampled` and `scope.coverage` identify sampling, and `truncated` is
true when the source graph was sampled even if no match was found. A context result
with `node_status: "not_in_sample"` is not evidence that the node does not exist.
Use structured tools when complete filtering or aggregation is required.
When only a SQLite index is available, results are limited to its indexed
data; they cannot recover source records absent from that index. The `scope` object
reports `source`, `scanned_nodes`, `available_nodes`, `declared_nodes`, and
`complete`. `complete: false` identifies a partial source (including older sampled
indexes); `complete: null` means completeness cannot be verified. Check its
`warning` before treating totals as exhaustive. Response counts describe matches
before pagination; for structured queries, `truncated` describes omitted result rows.

## Property joins

`mo_join_by_property` accepts `inner`, `left`, `right`, `outer`, and `full` joins.
Each side requires `pack_id` and `join_field`; the join field is included even
when omitted from `fields`, and dotted paths retain their full key for matching.
The existing response groups right-hand matches per left row rather than expanding
a Cartesian set of pairs. `limit: 0` returns no rows, and `truncated` reports omitted
left rows even when they share one join key.

## Verification

Self-contained regressions use temporary packs/databases, without Drive or production
sample data:

```sh
python -m pytest tests/test_graph_builders.py tests/test_structured_query.py tests/test_pack_search.py -q
node --test src/graphModel.test.mjs tests/graph-references.test.mjs
npm run build
```

The AI answer highlight resolves graph-context node IDs
within the queried pack, with a scoped text fallback only for older responses.
The AI question panel names its single target pack. Local-only graphs cannot be
queried through that remote-pack endpoint, so the panel asks you to select a
project pack instead of silently querying unrelated data.
