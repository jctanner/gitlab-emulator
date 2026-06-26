const CDN = "https://esm.sh";

async function loadCodeMirror() {
  const [
    codemirror,
    commands,
    language,
    highlight,
    state,
    view,
    yamlLanguage,
  ] = await Promise.all([
    import(`${CDN}/codemirror@6.0.1`),
    import(`${CDN}/@codemirror/commands@6.8.1`),
    import(`${CDN}/@codemirror/language@6.11.3`),
    import(`${CDN}/@lezer/highlight@1.2.1`),
    import(`${CDN}/@codemirror/state@6.5.2`),
    import(`${CDN}/@codemirror/view@6.38.5`),
    import(`${CDN}/@codemirror/lang-yaml@6.1.2`),
  ]);

  const yamlHighlightStyle = language.HighlightStyle.define([
    { tag: highlight.tags.comment, color: "#6e7781", fontStyle: "italic" },
    { tag: highlight.tags.propertyName, color: "#953800" },
    { tag: highlight.tags.string, color: "#0a3069" },
    { tag: highlight.tags.number, color: "#0550ae" },
    { tag: highlight.tags.bool, color: "#0550ae", fontWeight: "600" },
    { tag: highlight.tags.null, color: "#0550ae", fontWeight: "600" },
    { tag: highlight.tags.atom, color: "#0550ae" },
    { tag: highlight.tags.keyword, color: "#cf222e", fontWeight: "600" },
    { tag: highlight.tags.operator, color: "#cf222e" },
    { tag: highlight.tags.definitionKeyword, color: "#8250df", fontWeight: "600" },
    { tag: highlight.tags.meta, color: "#8250df" },
    { tag: highlight.tags.variableName, color: "#24292f" },
  ]);

  return {
    basicSetup: codemirror.basicSetup,
    indentWithTab: commands.indentWithTab,
    indentUnit: language.indentUnit,
    syntaxHighlighting: language.syntaxHighlighting,
    EditorState: state.EditorState,
    EditorView: view.EditorView,
    keymap: view.keymap,
    yamlHighlightStyle,
    yaml: yamlLanguage.yaml,
  };
}

function editorHeight(textarea) {
  const rows = Number.parseInt(textarea.getAttribute("rows") || "", 10);
  if (Number.isFinite(rows) && rows > 0) {
    return `${Math.max(260, rows * 21)}px`;
  }
  return textarea.classList.contains("ci-lab-editor") ? "420px" : "520px";
}

function enhanceTextarea(textarea, cm) {
  if (textarea.dataset.codeMirrorReady === "true") {
    return;
  }
  textarea.dataset.codeMirrorReady = "true";

  const wrapper = document.createElement("div");
  wrapper.className = "yaml-editor-shell";
  wrapper.style.setProperty("--yaml-editor-height", editorHeight(textarea));
  textarea.insertAdjacentElement("afterend", wrapper);

  const view = new cm.EditorView({
    parent: wrapper,
    state: cm.EditorState.create({
      doc: textarea.value,
      extensions: [
        cm.basicSetup,
        cm.yaml(),
        cm.syntaxHighlighting(cm.yamlHighlightStyle),
        cm.indentUnit.of("  "),
        cm.keymap.of([cm.indentWithTab]),
        cm.EditorView.lineWrapping,
        cm.EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            textarea.value = update.state.doc.toString();
          }
        }),
      ],
    }),
  });
  textarea.classList.add("yaml-editor-source");

  const sync = () => {
    textarea.value = view.state.doc.toString();
  };
  const form = textarea.form;
  if (form) {
    form.addEventListener("submit", sync);
    form.addEventListener("formdata", sync);
  }
}

async function initYamlEditors() {
  const textareas = Array.from(
    document.querySelectorAll('textarea[data-code-editor="yaml"]'),
  );
  if (textareas.length === 0) {
    return;
  }

  try {
    const cm = await loadCodeMirror();
    textareas.forEach((textarea) => enhanceTextarea(textarea, cm));
  } catch (error) {
    console.warn("CodeMirror YAML editor failed to load; using textarea fallback.", error);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initYamlEditors);
} else {
  initYamlEditors();
}
