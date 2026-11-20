// Applies the saved theme before first paint (external file: the CSP forbids inline scripts).
try { var t = localStorage.getItem("ol-theme"); document.documentElement.dataset.theme = t === "light" || t === "dark" ? t : "dark"; } catch (e) { document.documentElement.dataset.theme = "dark"; }
