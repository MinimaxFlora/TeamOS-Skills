#!/usr/bin/env node
const { validateAuthoringArgs, validateAuthoringResult, saveAuthoringArtifacts } = require("./authoring.cjs");
const { CWClient, normalizeAssetResult, downloadAssetsLocally, printJson } = require("./cw_client.cjs");

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("-")) {
      continue;
    }
    const key = token;
    const next = argv[i + 1];
    const value = next && !next.startsWith("-") ? next : "true";
    args[key] = value;
    if (value !== "true") {
      i += 1;
    }
  }
  return args;
}

const { validateBasePalette, COLOR_NAME_MAP, STYLE_PRESET_ENUM } = require("./palette.cjs");

function normalizeGenerationResult(result) {
  if (result.status === "ok" && Array.isArray(result.choices)) {
    result.choices = result.choices.map(choice => {
      let normalized = normalizeAssetResult(choice);
      if (!normalized.svg_url && !normalized.html_url && !normalized.svg_content && !normalized.html_content) {
        normalized.message = "由于图表极为复杂，当前已进入后台专家队列进行深度处理。请告知用户图表正在处理中，并立即主动运行 `node scripts/recompile_contextweave.cjs --session_id <session_id>` 拉取结果（脚本内置自动轮询），不要让用户手动触发。";
        normalized.svg_url = "WAITING_FOR_EXPERT_PROCESSING";
      }
      return normalized;
    });
    if (!result.session_id) {
      return {
        status: "error",
        error: {
          code: "MISSING_SESSION_ID",
          message: "生成成功响应缺少 session_id，无法用于后续编辑",
          recoverable: true,
          recovery_hint: "请重新执行生成；若仍失败请检查后端服务",
        },
        raw_result: result,
      };
    }
    return result;
  }

  result = normalizeAssetResult(result);
  if (result.status === "ok" && !result.session_id) {
    return {
      status: "error",
      error: {
        code: "MISSING_SESSION_ID",
        message: "生成成功响应缺少 session_id，无法用于后续编辑",
        recoverable: true,
        recovery_hint: "请重新执行生成；若仍失败请检查后端服务",
      },
      raw_result: result,
    };
  }
  if (result.status === "ok" && !result.svg_url && !result.html_url && !result.svg_content && !result.html_content) {
    result.message = "由于图表极为复杂，当前已进入后台专家队列进行深度处理。请告知用户图表正在处理中，并立即主动运行 `node scripts/recompile_contextweave.cjs --session_id <session_id>` 拉取结果（脚本内置自动轮询，默认最长等待约 20 分钟，生成完成即返回），不要让用户手动触发。";
    result.svg_url = "WAITING_FOR_EXPERT_PROCESSING";
  }
  return result;
}

async function main(argv = process.argv.slice(2), Client = CWClient) {
  const args = parseArgs(argv);
  const authoringError = validateAuthoringArgs(args);
  if (authoringError) { printJson(authoringError); process.exit(1); }
  const userRequest = args["--user_request"] || args["-u"];
  const inputFile = args["--input_file"] || args["-i"];
  const authoringFile = args["--authoring_file"];
  const diagramType = args["--diagram_type"];
  const outlineFile = args["--outline_file"];
  const sessionId = args["--session_id"] || args["-s"];
  const inputSequenceRaw = args["--input_sequence"];
  const enablePlan = args["--enable_plan"] === "true";
  const diagramStyle = args["--diagram_style"] || args["-d"];
  const morphology = args["--morphology"] || args["-m"];
  const accentTargetsRaw = args["--accent_targets"];
  const basePaletteRaw = args["--base_palette"];
  const outputName = args["--output_name"] || args["-n"];
  const outputDir = args["--output_dir"] || args["-o"];
  const n = parseInt(args["--n"] || "1", 10);
  const topK = parseInt(args["--top_k"] || "1", 10);
  let coDesignRevision = null, coDesignEditPaths = null, coDesignUpstreamUsage = null;
  try {
    if (args["--co_design_revision"] !== undefined) {
      const value = args["--co_design_revision"];
      if (typeof value !== "string" || !/^\d+$/.test(value)) throw Error("revision must be a nonnegative integer");
      coDesignRevision = Number(value);
      if (!Number.isSafeInteger(coDesignRevision)) throw Error("revision is too large");
    }
    if (args["--co_design_edit_paths"]) coDesignEditPaths = JSON.parse(args["--co_design_edit_paths"]);
    if (args["--co_design_upstream_usage"]) coDesignUpstreamUsage = JSON.parse(args["--co_design_upstream_usage"]);
  } catch(error) {
    printJson({status:"error",error:{code:"INVALID_CO_DESIGN_OPTIONS",message:String(error.message)}});
    process.exit(1);
  }

  if (!userRequest && !inputFile && !authoringFile && !outlineFile && !(sessionId && coDesignRevision !== null)) {
    printJson({
      status: "error",
      error: {
        code: "MISSING_INPUT",
        message: "必须提供 authoring_file、outline_file、user_request 或 input_file",
        recoverable: true,
        recovery_hint: "补充生成请求文本或输入文件后重试",
      },
    });
    process.exit(1);
  }

  let inputSequence = null;
  if (inputSequenceRaw) {
    try {
      inputSequence = JSON.parse(inputSequenceRaw);
    } catch (error) {
      printJson({
        status: "error",
        error: {
          code: "INVALID_INPUT_SEQUENCE",
          message: "input_sequence 必须是合法 JSON",
          recoverable: true,
          recovery_hint: "按 JSON 数组格式传参后重试",
        },
      });
      process.exit(1);
    }
  }

  let accentTargets = null;
  if (accentTargetsRaw) {
    try {
      accentTargets = JSON.parse(accentTargetsRaw);
    } catch (error) {
      printJson({
        status: "error",
        error: {
          code: "INVALID_ACCENT_TARGETS",
          message: "accent_targets 必须是合法 JSON",
          recoverable: true,
          recovery_hint: "按 JSON 数组格式传参后重试，如 [{\"name\":\"节点名\",\"color\":\"暖橙\"}]",
        },
      });
      process.exit(1);
    }
  }

  let basePalette = null;
  if (basePaletteRaw) {
    const validation = validateBasePalette(basePaletteRaw);
    if (validation.error) {
      printJson(validation.error);
      process.exit(1);
    }
    basePalette = validation.value;
  }

  const client = new Client();
  const rawResult = await client.runGeneration({
    userRequest,
    inputFile,
    diagramType,
    authoringFile,
    outlineFile,
    sessionId,
    inputSequence,
    validateRequestLength: true,
    diagramStyle,
    morphology,
    accentTargets,
    basePalette,
    useDefaultPalette: args["--use_default_palette"] === "true",
    enablePlan,
    n,
    topK,
    coDesignRevision,
    coDesignEditPaths,
    coDesignUpstreamUsage,
  });

  const result = normalizeGenerationResult(authoringFile ? validateAuthoringResult(rawResult, "svg") : rawResult);
  await saveAuthoringArtifacts(client, result, { outputName, outputDir, saveSource: true });

  if (result.status === "ok" && Array.isArray(result.choices)) {
    const fs = require("fs");
    const path = require("path");


    for (let i = 0; i < result.choices.length; i++) {
      const choice = result.choices[i];
      const suffix = `_choice_${i + 1}`;

      if (choice.cw_code) {
        const baseName = outputName || result.session_id || "diagram";
        const filename = `${baseName}${suffix}.cw`;
        let targetDir = process.cwd();
        if (outputDir) {
          targetDir = path.resolve(outputDir);
          if (!fs.existsSync(targetDir)) {
            fs.mkdirSync(targetDir, { recursive: true });
          }
        }
        const filePath = path.join(targetDir, filename);

        let finalCode = choice.cw_code;
        // Optionally inject choice-specific session id if backend provides it, otherwise use root session_id
        const choiceSessionId = choice.session_id || result.session_id;
        if (choiceSessionId) {
          finalCode = `# session_id: ${choiceSessionId}\n` + finalCode;
        }
        fs.writeFileSync(filePath, finalCode, "utf8");

        delete choice.cw_code;
        choice.saved_cw_file = filePath;
      }

      // Download assets for this choice
      const tempObj = {
        status: choice.status || "ok",
        artifacts: choice.artifacts,
        receipt: choice.receipt || result.receipt,
        session_id: choice.session_id || result.session_id,
        output_name: outputName ? `${outputName}${suffix}` : `${result.session_id || "diagram"}${suffix}`,
        output_dir: outputDir,
        html_content: choice.html_content,
        svg_content: choice.svg_content,
        html_url: choice.html_url,
        raw_svg_url: choice.raw_svg_url,
        svg_url: choice.svg_url,
        pptx_url: choice.pptx_url,
      };
      Object.defineProperty(tempObj, "_artifactAuth", { value: choice._artifactAuth || result._artifactAuth, enumerable: false });
      await downloadAssetsLocally(tempObj, client);

      if (tempObj.saved_html_file) { choice.saved_html_file = tempObj.saved_html_file; delete choice.html_content; }
      if (tempObj.saved_svg_file) { choice.saved_svg_file = tempObj.saved_svg_file; delete choice.svg_content; }
      if (tempObj.warnings?.length) choice.warnings = [...(choice.warnings || []), ...tempObj.warnings];
      if (tempObj.saved_pptx_file) choice.saved_pptx_file = tempObj.saved_pptx_file;
      if (tempObj.message) choice.message = tempObj.message;
    }
  } else if (result.status === "ok" && result.cw_code) {
    const fs = require("fs");
    const path = require("path");

    const filename = outputName ? `${outputName}.cw` : (result.session_id ? `${result.session_id}.cw` : "diagram.cw");
    let targetDir = process.cwd();
    if (outputDir) {
      targetDir = path.resolve(outputDir);
      if (!fs.existsSync(targetDir)) {
        fs.mkdirSync(targetDir, { recursive: true });
      }
    }
    const filePath = path.join(targetDir, filename);

    let finalCode = result.cw_code;
    if (result.session_id) {
      finalCode = `# session_id: ${result.session_id}\n` + finalCode;
    }
    fs.writeFileSync(filePath, finalCode, "utf8");

    // Remove cw_code from the output to prevent polluting LLM context window
    delete result.cw_code;
    result.saved_cw_file = filePath;
  }


  if (!Array.isArray(result.choices)) {
    result.output_name = outputName;
    result.output_dir = outputDir;
    await downloadAssetsLocally(result, client);
  }

  printJson(result);
  if (result.status === "error") {
    process.exit(1);
  }
}

module.exports = { validateBasePalette, COLOR_NAME_MAP, STYLE_PRESET_ENUM, main };

if (require.main === module) {
  main();
}
