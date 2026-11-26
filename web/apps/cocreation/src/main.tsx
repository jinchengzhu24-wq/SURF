import "@sokoban/ui-core/styles/tokens.css";
import "./styles/cocreation.css";
import { render } from "preact";
import { App } from "./App";

const mount = document.getElementById("app");
if (mount) {
  render(<App />, mount);
}
