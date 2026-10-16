/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html", "./static/js/**/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        paper: "#f8f8f6",
        ink: {
          DEFAULT: "#16181d",
          50: "#f5f6f7",
          100: "#eceef1",
          200: "#dcdfe4",
          300: "#c3c8d0",
          400: "#8f96a3",
          500: "#6b7280",
          600: "#4b5260",
          700: "#353b47",
          800: "#232730",
          900: "#181b22",
          950: "#0f1115",
        },
        accent: {
          DEFAULT: "#3451d1",
          50: "#eef1fd",
          300: "#93a5f5",
          600: "#2b45b8",
          700: "#24399a",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgb(16 24 40 / 0.04), 0 8px 24px -12px rgb(16 24 40 / 0.12)",
      },
    },
  },
  plugins: [],
};
