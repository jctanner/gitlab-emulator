// Syntax highlighting for the file viewer (table.blob-viewer).
//
// The server renders each line in its own table row. highlight.js works on
// the whole file so multi-line comments and strings are coloured correctly,
// and the result is split back into lines, re-opening and closing spans at
// each line break so every row is valid HTML on its own.
(function () {
  "use strict";

  var MAX_CHARS = 500000; // leave very large files as plain text

  var FILENAMES = {
    makefile: "makefile",
    gnumakefile: "makefile",
    dockerfile: "dockerfile",
    containerfile: "dockerfile",
    ".bashrc": "bash",
    ".bash_profile": "bash",
    ".profile": "bash",
    ".zshrc": "zsh",
    ".gitignore": "plaintext",
  };
  var SHEBANG = [
    [/\b(ba|z|k|da)?sh\b/, "bash"],
    [/\bpython/, "python"],
    [/\b(node|nodejs)\b/, "javascript"],
    [/\bruby\b/, "ruby"],
    [/\bperl\b/, "perl"],
  ];

  function languageFor(path, firstLine) {
    var name = path.split("/").pop().toLowerCase();
    if (FILENAMES[name]) return FILENAMES[name];
    if (/^(dockerfile|containerfile)\./.test(name)) return "dockerfile";
    var dot = name.lastIndexOf(".");
    if (dot > 0) {
      var ext = name.slice(dot + 1);
      if (window.hljs.getLanguage(ext)) return ext;
    }
    if (firstLine && firstLine.indexOf("#!") === 0) {
      for (var i = 0; i < SHEBANG.length; i++) {
        if (SHEBANG[i][0].test(firstLine)) return SHEBANG[i][1];
      }
    }
    return null;
  }

  // Split highlighted HTML into one string per source line. Spans that are
  // open at a line break are closed there and re-opened on the next line.
  function splitLines(html) {
    var tag = /<span[^>]*>|<\/span>/g;
    var open = [];
    return html.split("\n").map(function (text) {
      var line = open.join("") + text;
      var match;
      tag.lastIndex = 0;
      while ((match = tag.exec(text)) !== null) {
        if (match[0] === "</span>") open.pop();
        else open.push(match[0]);
      }
      return line + open.map(function () { return "</span>"; }).join("");
    });
  }

  function highlight(table) {
    var cells = Array.prototype.slice.call(
      table.querySelectorAll(".blob-code-inner")
    );
    var code = cells.map(function (cell) { return cell.textContent; }).join("\n");
    if (!code || code.length > MAX_CHARS) return;

    var language = languageFor(table.getAttribute("data-highlight-path") || "", cells[0] && cells[0].textContent);
    if (!language || language === "plaintext" || !window.hljs.getLanguage(language)) return;

    var lines = splitLines(
      window.hljs.highlight(code, {language: language, ignoreIllegals: true}).value
    );
    if (lines.length !== cells.length) return; // never misalign rows
    cells.forEach(function (cell, index) { cell.innerHTML = lines[index]; });
    table.classList.add("hljs");
    table.setAttribute("data-highlighted", language);
  }

  function init() {
    if (!window.hljs) return;
    var table = document.querySelector("table.blob-viewer[data-highlight-path]");
    if (table) highlight(table);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
