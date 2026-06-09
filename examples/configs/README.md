# Configuration Examples

This directory contains real-world configuration files tuned for specific use cases.

## `strict.json`

A configuration for edge-specificity tuning used to break up a mega-cluster on a game-server API repository. It uses strict directory depth (`min_dir_depth: 3`), capped cluster size (`cluster_cap: 8`), and supplementary stopwords and basenames to prevent overly generic files and tokens from bridging unrelated commits together.

For more information about cluster tuning and how to diagnose and fix mega-collapse issues, see the "Troubleshooting: Clustering Mega-Collapse" section in the main README.
