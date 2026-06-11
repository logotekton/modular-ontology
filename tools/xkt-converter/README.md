# Modular XKT Converter

Windows용 IFC to XKT 변환 앱입니다. 관리자가 Google Drive에 올리기 전에 IFC 모델을 xeokit 뷰어용 `.xkt` 파일로 변환할 때 사용합니다.

## 실행

```powershell
.\tools\xkt-converter\publish\ModularXktConverter.exe
```

또는 `ModularXktConverter.exe`를 더블클릭합니다.

## 사용 방법

1. IFC 파일 또는 IFC가 들어 있는 폴더를 앱 창에 드래그합니다.
2. 필요하면 `하위 폴더까지 변환`, `기존 XKT 덮어쓰기` 옵션을 선택합니다.
3. 출력 폴더를 지정합니다. 비워두면 IFC 파일과 같은 폴더에 `.xkt`가 생성됩니다.
4. `XKT 변환 시작`을 누릅니다.
5. 생성된 `.xkt` 파일을 Google Drive의 해당 프로젝트 `ifc-models` 폴더에 IFC와 함께 올립니다.

## Build

```powershell
dotnet publish .\tools\xkt-converter\ModularXktConverter.csproj -c Release -r win-x64 --self-contained true -o .\tools\xkt-converter\publish
```

## 내부 동작

앱은 먼저 현재 프로젝트의 `node_modules/@xeokit/xeokit-convert/convert2xkt.js`를 찾습니다. 찾지 못하면 `npx -y @xeokit/xeokit-convert@1.3.2`로 실행합니다.

따라서 변환 실행에는 Node.js 또는 npx가 필요합니다.
