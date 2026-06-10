import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");

const edgeProgramFiles = [
  "node_modules/@xeokit/xeokit-sdk/dist/xeokit-sdk.es.js",
  "node_modules/@xeokit/xeokit-sdk/dist/xeokit-sdk.cjs.js",
  "node_modules/@xeokit/xeokit-sdk/src/viewer/scene/model/layer/programs/EdgesProgram.js",
];

function patchEdgeProgram(filePath) {
  let source = fs.readFileSync(filePath, "utf8");
  if (!source.includes('createUniform("vec2", "edgeOffset"')) {
    source = source.replace(
      '    const edgeColorUniform = colorUniform && programVariables.createUniform("vec4", "edgeColor", (set, state) => set(state.legacyFrameCtx.programColor));',
      '    const edgeColorUniform = colorUniform && programVariables.createUniform("vec4", "edgeColor", (set, state) => set(state.legacyFrameCtx.programColor));\n    const edgeOffset = programVariables.createUniform("vec2", "edgeOffset", (set, state) => set(state.legacyFrameCtx.edgeOffset || [0, 0]));',
    );
  }
  if (!source.includes('createUniform("float", "edgeDepthBias"')) {
    source = source.replace(
      '    const edgeOffset = programVariables.createUniform("vec2", "edgeOffset", (set, state) => set(state.legacyFrameCtx.edgeOffset || [0, 0]));',
      '    const edgeOffset = programVariables.createUniform("vec2", "edgeOffset", (set, state) => set(state.legacyFrameCtx.edgeOffset || [0, 0]));\n    const edgeDepthBias = programVariables.createUniform("float", "edgeDepthBias", (set, state) => set(state.legacyFrameCtx.edgeDepthBias || 0));',
    );
  }
  if (!source.includes("        edgeOffset,\n        edgeDepthBias,\n        getLogDepth")) {
    source = source.replace(
      '        programName: colorUniform ? "Edges" : "EdgesColor",\n        getLogDepth:',
      '        programName: colorUniform ? "Edges" : "EdgesColor",\n        edgeOffset,\n        edgeDepthBias,\n        getLogDepth:',
    );
    source = source.replace(
      '        programName: colorUniform ? "Edges" : "EdgesColor",\n        edgeOffset,\n        getLogDepth:',
      '        programName: colorUniform ? "Edges" : "EdgesColor",\n        edgeOffset,\n        edgeDepthBias,\n        getLogDepth:',
    );
  }
  fs.writeFileSync(filePath, source);
}

for (const relativePath of edgeProgramFiles) {
  const filePath = path.join(root, relativePath);
  if (fs.existsSync(filePath)) {
    patchEdgeProgram(filePath);
  }
}
