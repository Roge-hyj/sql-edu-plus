<template>
  <view class="lang-wrap">
    <view class="lang-trigger" @tap.stop="open = !open">
      <text class="lang-globe">🌐</text>
      <text class="lang-current">{{ currentLabel }}</text>
    </view>
    <view v-if="open" class="lang-menu" @tap.stop>
      <view
        v-for="opt in LANGUAGE_OPTIONS"
        :key="opt.value"
        class="lang-option"
        :class="{ active: opt.value === language }"
        @tap="choose(opt.value)"
      >
        <text>{{ opt.label }}</text>
        <text v-if="opt.value === language" class="lang-check">✓</text>
      </view>
    </view>
    <view v-if="open" class="lang-mask" @tap="open = false" />
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import {
  LANGUAGE_OPTIONS,
  language,
  setLanguage,
  type AiLanguage,
} from "@/utils/i18n";

const open = ref(false);

const currentLabel = computed(
  () => LANGUAGE_OPTIONS.find((o) => o.value === language.value)?.label ?? "简体中文",
);

function choose(value: AiLanguage) {
  setLanguage(value);
  open.value = false;
}
</script>

<style lang="scss" scoped>
.lang-wrap {
  position: relative;
}

.lang-trigger {
  display: flex;
  align-items: center;
  gap: 8rpx;
  padding: 10rpx 20rpx;
  background: #fff;
  border-radius: 999rpx;
  box-shadow: 0 4rpx 14rpx rgba(23, 29, 43, 0.08);
  font-size: 24rpx;
  color: $text-2;
}

.lang-globe {
  font-size: 24rpx;
}

.lang-menu {
  position: absolute;
  top: calc(100% + 12rpx);
  right: 0;
  min-width: 220rpx;
  background: #fff;
  border-radius: 18rpx;
  box-shadow: 0 16rpx 50rpx rgba(23, 29, 43, 0.14);
  padding: 10rpx;
  z-index: 120;
}

.lang-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18rpx 22rpx;
  border-radius: 12rpx;
  font-size: 26rpx;
  color: $text-1;

  &.active {
    background: $brand-soft;
    color: $brand-deep;
    font-weight: 600;
  }
}

.lang-check {
  color: $brand-deep;
  font-weight: 700;
}

.lang-mask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 110;
}
</style>
