/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../app/templates/**/*.html",
    "../app/static/**/*.html"
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        terminal: ['VT323', 'monospace'],
      },
      colors: {
        terminal: {
          black: '#000000',
          green: '#00ff00',
          'dim-green': '#00cc00',
          'dark-green': '#006600',
          gray: {
            dark: '#1a1a1a',
            mid: '#333333'
          },
          red: '#ff0000'
        }
      },
      boxShadow: {
        'retro': 'inset -2px -2px 0 0 #006600, inset 2px 2px 0 0 #00ff00',
        'retro-inverted': 'inset 2px 2px 0 0 #006600, inset -2px -2px 0 0 #00ff00'
      }
    }
  },
  plugins: [require("daisyui")],
  daisyui: {
    themes: [{
      terminal: {
        primary: "#00ff00",
        secondary: "#00cc00",
        accent: "#006600",
        neutral: "#000000",
        "base-100": "#000000",
        "base-200": "#001a00",
        "base-300": "#003300",
        info: "#00ff00",
        success: "#00ff00",
        warning: "#ffff00",
        error: "#ff0000",
      }
    }],
    base: false,
    styled: true,
    utils: true,
    prefix: "",
    logs: true,
    themeRoot: ":root"
  }
}