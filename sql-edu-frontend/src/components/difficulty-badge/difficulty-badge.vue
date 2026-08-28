<template>
  <view
    class="diff-badge"
    :style="{
      background: bgColor,
      color: color,
    }"
  >
    <text class="diff-num">{{ displayValue }}</text>
    <text v-if="showLabel" class="diff-label">{{ label }}</text>
  </view>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { difficultyColor, difficultyLabel, difficultyText } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    /** 1~10 难度值 */
    value: number | null | undefined;
    showLabel?: boolean;
  }>(),
  { showLabel: true },
);

const color = computed(() => difficultyColor(props.value));
const bgColor = computed(() => difficultyColor(props.value) + "1A");
const displayValue = computed(() => difficultyText(props.value));
const label = computed(() => difficultyLabel(props.value));
</script>

<style lang="scss" scoped>
.diff-badge {
  display: inline-flex;
  align-items: center;
  gap: 8rpx;
  padding: 4rpx 16rpx;
  border-radius: 999rpx;
  line-height: 1.6;
  flex-shrink: 0;
}

.diff-num {
  font-size: 22rpx;
  font-weight: 700;
  font-family: $font-mono;
}

.diff-label {
  font-size: 22rpx;
  font-weight: 500;
}
</style>
