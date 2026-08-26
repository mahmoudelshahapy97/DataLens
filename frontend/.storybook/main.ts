import type { StorybookConfig } from '@storybook/react-vite';

/**
 * Storybook, on the React framework.
 *
 * This pointed at `@storybook/web-components-vite` when the only components were
 * Lit elements. The design system is React now, and the one surviving Lit element
 * -- <plotly-chart> -- has a React wrapper, so it renders here through that.
 *
 * Storybook 9 rather than 8.6: 8.6 peers `vite@^4 || ^5 || ^6` and will not
 * install alongside Vite 7. It also folds addon-essentials, addon-actions and
 * addon-controls into the core package, which is why the addon list is one entry.
 */
const config: StorybookConfig = {
  stories: ['../src/**/*.stories.@(js|jsx|mjs|ts|tsx)'],
  addons: ['@storybook/addon-docs'],
  framework: {
    name: '@storybook/react-vite',
    options: {},
  },
  typescript: {
    check: false,
    reactDocgen: 'react-docgen-typescript',
    reactDocgenTypescriptOptions: {
      shouldExtractLiteralValuesFromEnum: true,
      propFilter: (prop) => (prop.parent ? !/node_modules/.test(prop.parent.fileName) : true),
    },
  },
};

export default config;
