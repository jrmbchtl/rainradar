import { nodeResolve } from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";
import terser from "@rollup/plugin-terser";

const DIST = "custom_components/rainradar/frontend/dist";

export default {
  input: "custom_components/rainradar/frontend/src/rainradar-card.js",
  output: {
    file: `${DIST}/rainradar-card.js`,
    format: "iife",
    name: "RainradarCard",
    sourcemap: false,
  },
  plugins: [
    nodeResolve(),
    commonjs(),
    terser({
      format: {
        comments: false,
      },
    }),
  ],
};
