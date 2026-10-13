import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { browserApi } from "./api/client";
import "./styles.css";

const root = document.getElementById("root");
if (root !== null) {
  createRoot(root).render(
    <StrictMode>
      <App api={browserApi()} />
    </StrictMode>,
  );
}
