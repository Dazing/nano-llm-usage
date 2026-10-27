<template>
  <section class="pnl pnl-empty" data-testid="empty-state" role="status" aria-label="No series collected yet">
    <div class="in">
      <svg class="radar" viewBox="0 0 120 120" aria-hidden="true">
        <circle class="ring" cx="60" cy="60" r="52" />
        <circle class="ring" cx="60" cy="60" r="34" />
        <circle class="ring" cx="60" cy="60" r="16" />
        <path class="cross" d="M60 6V114M6 60H114" />
        <path class="sweep" d="M60 60L104 34" />
        <circle class="blip" cx="86" cy="44" r="3" />
      </svg>
      <p class="e1">No series collected yet.</p>
      <p class="e2">Collection starts after the first successful sample.</p>
      <span class="chip is-idle"><span class="dot"></span>Collecting</span>
    </div>
  </section>
</template>
