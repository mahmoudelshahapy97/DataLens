import type { Preview } from '@storybook/react-vite';

import '../src/styles/tailwind.css';

/**
 * Every story renders twice: once light, once dark.
 *
 * The product ships both themes and the token layer is the thing most likely to
 * be wrong in one of them -- a colour hard-coded in a component looks correct
 * until somebody flips the theme. Rendering both side by side makes that visible
 * at the moment the component is written rather than in a screenshot run later.
 *
 * `dir` is a toolbar control for the same reason: the interface mirrors for
 * Arabic, and a component that uses `left`/`right` instead of `start`/`end`
 * survives review and then breaks the Arabic build.
 */
const preview: Preview = {
  parameters: {
    controls: {
      matchers: { color: /(background|color)$/i, date: /Date$/i },
    },
  },

  globalTypes: {
    direction: {
      description: 'Writing direction',
      defaultValue: 'ltr',
      toolbar: {
        title: 'Direction',
        icon: 'transfer',
        items: [
          { value: 'ltr', title: 'LTR' },
          { value: 'rtl', title: 'RTL (Arabic)' },
        ],
        dynamicTitle: true,
      },
    },
  },

  decorators: [
    (Story, context) => {
      const dir = context.globals.direction as 'ltr' | 'rtl';
      document.documentElement.setAttribute('dir', dir);

      return (
        <div dir={dir} className="grid gap-4 md:grid-cols-2">
          {(['light', 'dark'] as const).map((theme) => (
            <div
              key={theme}
              data-theme={theme}
              className="rounded-lg border border-border bg-background p-5 text-foreground"
            >
              <p className="mb-3 text-[0.6875rem] font-semibold uppercase tracking-wider text-muted-foreground">
                {theme}
              </p>
              <Story />
            </div>
          ))}
        </div>
      );
    },
  ],
};

export default preview;
