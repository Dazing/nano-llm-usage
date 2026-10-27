import { createApp } from "vue";
import App from "./App.vue";
import "./styles/tokens.css";
import "./styles/app.css";

// An absent stored theme leaves data-theme unset so the system theme applies.
const stored = localStorage.getItem("jarvis-theme");
if (stored === "light" || stored === "dark") {
  document.documentElement.dataset.theme = stored;
}

createApp(App).mount("#app");
