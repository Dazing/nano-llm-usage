// Registers probe_loader.mjs for the disposable Node probes in this directory.
import { register } from "node:module";
import { pathToFileURL } from "node:url";

register("./probe_loader.mjs", pathToFileURL("./"));
