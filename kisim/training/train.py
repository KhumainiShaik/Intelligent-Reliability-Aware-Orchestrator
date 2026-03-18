"""
Train a Q-learning policy using KISim episodes.

Usage:
    python -m training.train --episodes 50000 --seed 42
"""

import argparse
import json
import logging
import os
from datetime import datetime

from kisim.training.q_learning import QLearningTrainer

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Train RL policy for orchestrated rollouts")
    parser.add_argument("--episodes", type=int, default=50000, help="Number of training episodes")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--alpha", type=float, default=0.1, help="Learning rate")
    parser.add_argument("--epsilon", type=float, default=0.3, help="Initial exploration rate")
    parser.add_argument("--epsilon-decay", type=float, default=0.9995, help="Epsilon decay rate")
    parser.add_argument("--output", type=str, default="artifacts", help="Output directory")
    parser.add_argument("--version", type=str, default=None, help="Policy version tag")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )

    version = args.version or f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir = os.path.join(args.output, version)

    logger.info("=" * 60)
    logger.info("KISim RL Training Pipeline")
    logger.info("  Episodes:  %d", args.episodes)
    logger.info("  Seed:      %d", args.seed)
    logger.info("  Alpha:     %s", args.alpha)
    logger.info("  Epsilon:   %s (decay=%s)", args.epsilon, args.epsilon_decay)
    logger.info("  Output:    %s", output_dir)
    logger.info("  Version:   %s", version)
    logger.info("=" * 60)

    trainer = QLearningTrainer(
        alpha=args.alpha,
        epsilon=args.epsilon,
        epsilon_decay=args.epsilon_decay,
        seed=args.seed,
    )

    summary = trainer.train(num_episodes=args.episodes, seed=args.seed)

    # Export artifact
    trainer.export_artifact(output_dir, version)

    # Save training summary
    summary_path = os.path.join(output_dir, "training_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("Training summary saved to %s", summary_path)

    logger.info("Training Summary:")
    logger.info("  Total episodes:     %d", summary["total_episodes"])
    logger.info("  Unique states:      %d", summary["unique_states"])
    logger.info("  Mean cost:          %.4f", summary["mean_cost"])
    logger.info("  Final mean cost:    %.4f", summary["final_mean_cost"])
    logger.info("  Action distribution:")
    for action, pct in summary["action_distribution"].items():
        logger.info("    %12s: %.2f%%", action, pct * 100)


if __name__ == "__main__":
    main()
