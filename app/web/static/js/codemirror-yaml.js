const CDN = "https://esm.sh";
const STATE_VERSION = "@codemirror/state@6.5.2";
const VIEW_VERSION = "@codemirror/view@6.38.5";
const LANGUAGE_VERSION = "@codemirror/language@6.11.3";
const HIGHLIGHT_VERSION = "@lezer/highlight@1.2.1";
const CODEMIRROR_DEPS = `?deps=${STATE_VERSION},${VIEW_VERSION},${LANGUAGE_VERSION},${HIGHLIGHT_VERSION}`;

async function loadCodeMirror() {
  const [
    commands,
    language,
    highlight,
    state,
    view,
    yamlLanguage,
  ] = await Promise.all([
    import(`${CDN}/@codemirror/commands@6.8.1${CODEMIRROR_DEPS}`),
    import(`${CDN}/${LANGUAGE_VERSION}${CODEMIRROR_DEPS}`),
    import(`${CDN}/${HIGHLIGHT_VERSION}`),
    import(`${CDN}/@codemirror/state@6.5.2`),
    import(`${CDN}/${VIEW_VERSION}${CODEMIRROR_DEPS}`),
    import(`${CDN}/@codemirror/lang-yaml@6.1.2${CODEMIRROR_DEPS}`),
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

  const baseSetup = [
    view.lineNumbers(),
    view.highlightActiveLineGutter(),
    commands.history(),
    view.drawSelection(),
    language.bracketMatching(),
    language.indentUnit.of("  "),
    view.EditorView.lineWrapping,
    view.keymap.of([
      commands.indentWithTab,
      ...commands.defaultKeymap,
      ...commands.historyKeymap,
    ]),
  ];

  return {
    baseSetup,
    syntaxHighlighting: language.syntaxHighlighting,
    EditorState: state.EditorState,
    EditorView: view.EditorView,
    Decoration: view.Decoration,
    MatchDecorator: view.MatchDecorator,
    ViewPlugin: view.ViewPlugin,
    yamlHighlightStyle,
    yaml: yamlLanguage.yaml,
  };
}

function yamlTokenDecorator(cm, regexp, className) {
  const matcher = new cm.MatchDecorator({
    regexp,
    decoration: cm.Decoration.mark({ class: className }),
  });

  return cm.ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = matcher.createDeco(view);
      }

      update(update) {
        this.decorations = matcher.updateDeco(update, this.decorations);
      }
    },
    {
      decorations: (plugin) => plugin.decorations,
    },
  );
}

function yamlTokenHighlighter(cm) {
  return [
    yamlTokenDecorator(cm, /^[ \t]*(?:-\s*)?[A-Za-z_][A-Za-z0-9_.-]*(?=\s*:)/gm, "cm-yaml-key"),
    yamlTokenDecorator(cm, /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, "cm-yaml-string"),
    yamlTokenDecorator(cm, /\b(?:true|false|null|~)\b/g, "cm-yaml-literal"),
    yamlTokenDecorator(cm, /\b\d+(?:\.\d+)?\b/g, "cm-yaml-number"),
    yamlTokenDecorator(cm, /#.*/g, "cm-yaml-comment"),
  ];
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
        ...cm.baseSetup,
        cm.yaml(),
        cm.syntaxHighlighting(cm.yamlHighlightStyle),
        ...yamlTokenHighlighter(cm),
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

function enhanceViewer(viewer, cm) {
  if (viewer.dataset.codeMirrorReady === "true") {
    return;
  }
  viewer.dataset.codeMirrorReady = "true";

  const wrapper = document.createElement("div");
  wrapper.className = "yaml-viewer-shell";
  viewer.insertAdjacentElement("afterend", wrapper);

  new cm.EditorView({
    parent: wrapper,
    state: cm.EditorState.create({
      doc: viewer.textContent || "",
      extensions: [
        cm.EditorView.editable.of(false),
        cm.EditorState.readOnly.of(true),
        ...cm.baseSetup,
        cm.yaml(),
        cm.syntaxHighlighting(cm.yamlHighlightStyle),
        ...yamlTokenHighlighter(cm),
      ],
    }),
  });
  viewer.classList.add("yaml-viewer-source");
}

async function initYamlEditors() {
  const textareas = Array.from(
    document.querySelectorAll('textarea[data-code-editor="yaml"]'),
  );
  const viewers = Array.from(
    document.querySelectorAll('[data-code-viewer="yaml"]'),
  );
  if (textareas.length === 0 && viewers.length === 0) {
    return;
  }

  try {
    const cm = await loadCodeMirror();
    textareas.forEach((textarea) => enhanceTextarea(textarea, cm));
    viewers.forEach((viewer) => enhanceViewer(viewer, cm));
  } catch (error) {
    console.warn("CodeMirror YAML highlighting failed to load; using plain text fallback.", error);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initYamlEditors);
} else {
  initYamlEditors();
}
