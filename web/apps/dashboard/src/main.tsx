import "@sokoban/ui-core/styles/tokens.css";
import "./styles/dashboard.css";
import { render } from "preact";
import { App } from "./App";

const root = document.getElementById("app");
if (root) {
  render(<App />, root);
}
