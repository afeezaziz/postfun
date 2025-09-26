# Postfun Website

Postfun is a decentralized social finance platform that combines social media with financial incentives, allowing users to earn rewards for their engagement and content creation.

## Getting Started

### Prerequisites
- Node.js (v16 or higher)
- npm or yarn

### Installation
```bash
npm install
```

### Development
```bash
npm run dev
```
Starts the development server with Hot Module Replacement (HMR).

### Building for Production
```bash
npm run build
```
Creates a production-ready build in the `dist/` directory.

### Preview Production Build
```bash
npm run preview
```
Locally preview the production build.

### Deployment
```bash
npm run deploy
```
Builds and deploys to Cloudflare Pages via Wrangler.

## Project Structure
- `src/` - Main source code
- `src/components/` - Reusable React components
- `src/pages/` - Page components for each route
- `src/hooks/` - Custom React hooks
- `src/store/` - Zustand state management stores
- `src/lib/` - Utility libraries (API client, etc.)
- `src/assets/` - Static assets

## Tech Stack
- React 19.1.1 with Vite 7.1.2
- Tailwind CSS v4
- React Router DOM 7.8.1
- Zustand for global UI state
- TanStack Query for server state management
- Axios for API requests
- Recharts for data visualization