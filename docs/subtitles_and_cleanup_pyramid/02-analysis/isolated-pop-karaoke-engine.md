# Isolated Pop Karaoke Engine (Zero Sentence Jitter)

## Problem Statement
Standard inline font scaling (`\fs` or `\fscx`) shifts the horizontal layout of neighbouring words, causing the entire subtitle line to vibrate / dance left and right as each word is spoken.

## Solution Architecture

1. **Persistent Multi-Word Sentence Layout:**
   - For a grouped phrase of $N$ words, $N$ sequential `Dialogue` lines are generated.
   - Every line contains all $N$ words, ensuring the line length and baseline remain identical across the whole phrase duration.

2. **Isolated Active Word Styling:**
   - **Active Word ($j == i$):**
     - Primary highlight color: `\c{highlight_color}`
     - Outline color: `\3c{outline_color}`
     - Bold weight: `\b1`
     - Pop Animation: `\t(0,70,\fscy{pop})\t(70,140,\fscy100)` (vertical-only scaling; horizontal scaling `\fscx` remains 100%).
   - **Inactive Words ($j \neq i$):**
     - Base color: `\c{base_color}`
     - Outline color: `\3c{outline_color}`
     - Standard weight: `\b0`
     - Fixed scaling: `\fscx100\fscy100`

3. **Result:**
   - Active word illuminates and pops with high visual energy.
   - Non-active words remain 100% stationary without any pixel-level jitter.
