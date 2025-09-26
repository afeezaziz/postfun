# Postfun Retro Computer Style Guide

This document defines the visual style guidelines for the Postfun Wallet Extension and associated website. The design is inspired by 70s and 80s computer terminals and interfaces, creating a nostalgic yet functional aesthetic.

## Color Palette

### Primary Colors
- **Text/Green**: `#00ff00` - Classic CRT monitor green
- **Background/Black**: `#000000` - Pure black background
- **Surface/Dark Gray**: `#1a1a1a` - Dark gray for card backgrounds
- **Border/Medium Gray**: `#333333` - Medium gray for borders

### Secondary Colors
- **Text/Dim Green**: `#00cc00` - Slightly dimmer green for secondary text
- **Text/Dark Green**: `#006600` - Dark green for disabled states and shadows
- **Error/Red**: `#ff0000` - Bright red for error messages
- **Error/Dark Red**: `#cc0000` - Darker red for error shadows

## Typography

### Font Family
- **Primary Font**: `VT323` - Monospace font that mimics classic computer terminals
- **Fallback**: `monospace` - System monospace as fallback

### Font Sizes
- **Headers**: `16px` - Standard terminal text size
- **Body**: `16px` - Consistent sizing for readability
- **Small Text**: `14px` - For secondary information
- **Badges**: `12px` - For compact labels

## UI Components

### Buttons
Buttons use a classic 3D effect with inset and outset shadows to mimic physical buttons from retro computers.

```css
.retro-button {
  background-color: #000000;
  border: 2px solid #00ff00;
  color: #00ff00;
  font-family: 'VT323', monospace;
  padding: 4px 8px;
  box-shadow: inset -2px -2px 0 0 #006600, inset 2px 2px 0 0 #00ff00;
}

.retro-button:hover {
  background-color: #001a00;
}

.retro-button:active {
  box-shadow: inset 2px 2px 0 0 #006600, inset -2px -2px 0 0 #00ff00;
  background-color: #003300;
}
```

### Inputs
Input fields maintain the retro terminal aesthetic with solid borders and monospace fonts.

```css
.retro-input {
  background-color: #000000;
  border: 1px solid #00ff00;
  color: #00ff00;
  font-family: 'VT323', monospace;
  padding: 4px;
  box-shadow: inset 2px 2px 0 0 #000000, inset -2px -2px 0 0 #006600;
}

.retro-input:focus {
  outline: 1px solid #00ff00;
  background-color: #001a00;
}
```

### Cards
Cards use solid borders with inset shadows to create a recessed effect.

```css
.card {
  background-color: #000000;
  border: 1px solid #00ff00;
  box-shadow: inset 2px 2px 0 0 #000000, inset -2px -2px 0 0 #006600;
}
```

### Badges
Badges are compact labels with solid borders for status indicators.

```css
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 4px;
  font-size: 12px;
  font-family: 'VT323', monospace;
  border: 1px solid #00ff00;
  background-color: #000000;
  color: #00ff00;
}
```

## Status Indicators

### Connection Status
Status indicators use simple colored dots to show connection states.

```css
.status-indicator {
  display: inline-block;
  width: 8px;
  height: 8px;
  background-color: #00ff00;
  border: 1px solid #000000;
}

.status-indicator.connected {
  background-color: #00ff00;
  box-shadow: 0 0 5px #00ff00;
}

.status-indicator.disconnected {
  background-color: #ff0000;
}
```

## Animations & Effects

### Blinking Cursor
A classic blinking cursor effect for text inputs.

```css
.blinking-cursor::after {
  content: "▋";
  animation: blink 1s infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
```

### Loading Indicator
Simple spinner that matches the retro aesthetic.

```css
.retro-loading {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid #00ff00;
  border-top: 2px solid #000000;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}
```

### Scanlines Effect
Optional CRT scanlines effect for authentic monitor appearance.

```css
.scanlines {
  position: relative;
  overflow: hidden;
}

.scanlines::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: linear-gradient(rgba(0, 255, 0, 0.05) 50%, rgba(0, 0, 0, 0.25) 50%);
  background-size: 100% 4px;
  pointer-events: none;
  z-index: 1;
}
```

## Layout & Spacing

### Spacing System
- **Micro**: `2px` - For tight spacing between elements
- **Small**: `4px` - For padding and small gaps
- **Medium**: `8px` - For component padding
- **Large**: `16px` - For section spacing
- **X-Large**: `24px` - For major layout divisions

### Borders
- **Thin**: `1px` - For subtle divisions
- **Thick**: `2px` - For prominent borders and buttons

## Accessibility

### Contrast
All text colors meet minimum WCAG 2.1 contrast requirements against the black background:
- Green text on black: 15.3:1 contrast ratio
- Dim green text on black: 10.2:1 contrast ratio
- Dark green text on black: 4.1:1 contrast ratio (minimum acceptable)

### Focus States
All interactive elements have clear focus indicators using bright green outlines.

## Implementation Tips

1. **Consistency**: Maintain the monospace font and green-on-black color scheme throughout the interface
2. **Simplicity**: Avoid gradients, rounded corners, and modern UI effects
3. **Authenticity**: Reference actual 70s/80s computer interfaces for inspiration
4. **Functionality**: Prioritize usability while maintaining the retro aesthetic
5. **Performance**: Keep CSS simple and lightweight for fast rendering

This style guide ensures a consistent retro computing experience across all Postfun products while maintaining modern web standards and accessibility requirements.