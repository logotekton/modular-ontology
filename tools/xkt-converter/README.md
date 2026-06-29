# Modular Ontology Builder

Windows desktop utility for Modular Ontology administrators.

## Features

- IFC to XKT conversion for xeokit model viewing.
- Revit IFC to OpenCrab-compatible ontology pack ZIP.
- Advance Steel XML to OpenCrab BM25 ontology pack ZIP.
- Optional README/backdata inclusion.
- Optional LocalCrab ZIP generation for Advance Steel XML.
- Manus-style harness UI: staged execution, live log, progress, and local job history.

## Run

```powershell
.\tools\xkt-converter\publish\ModularOntologyBuilder.exe
```

## Build

```powershell
dotnet publish .\tools\xkt-converter\ModularXktConverter.csproj -c Release -r win-x64 --self-contained true -o .\tools\xkt-converter\publish
```

The published folder includes the Python pack builder scripts under `publish\scripts`.

## Runtime Dependencies

- XKT conversion requires Node.js or `npx`, unless `MODULAR_ONTOLOGY_XEOKIT_CONVERT_JS` points to `convert2xkt.js`.
- Pack generation requires Python. Set `MODULAR_ONTOLOGY_PYTHON` to a specific Python executable if needed.

## Output Workflow

1. Use `XKT 변환` to create `.xkt` next to an IFC file or in a selected output folder.
2. Use `팩 빌더` to create an ontology pack ZIP from Revit IFC or Advance Steel XML.
3. Upload the generated `.xkt`, source IFC, and ontology pack ZIP to the matching Google Drive project folders.
