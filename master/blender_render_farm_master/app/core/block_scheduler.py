from __future__ import annotations

from app.core.models import RenderBlock, RenderJob


class BlockScheduler:
    def _build_contiguous_groups(self, frames: list[int], block_size: int) -> list[list[int]]:
        sorted_frames = sorted(set(frames))
        if not sorted_frames:
            return []

        groups: list[list[int]] = []
        current_group: list[int] = []

        for frame in sorted_frames:
            if not current_group:
                current_group.append(frame)
                continue

            if frame == current_group[-1] + 1 and len(current_group) < block_size:
                current_group.append(frame)
                continue

            groups.append(current_group)
            current_group = [frame]

        if current_group:
            groups.append(current_group)

        return groups

    def _new_block_id(self, job: RenderJob, start_frame: int, end_frame: int) -> str:
        block_id = f"block_{job.nextBlockSequence:04d}_{start_frame:04d}_{end_frame:04d}"
        job.nextBlockSequence += 1
        return block_id

    def create_pending_blocks(self, job: RenderJob, frames: list[int], carry_attempt_count: int = 0) -> list[RenderBlock]:
        blocks: list[RenderBlock] = []
        for group in self._build_contiguous_groups(frames, job.blockSize):
            blocks.append(
                RenderBlock(
                    blockId=self._new_block_id(job, group[0], group[-1]),
                    startFrame=group[0],
                    endFrame=group[-1],
                    status="pending",
                    attemptCount=carry_attempt_count,
                )
            )
        return blocks

    def populate_job(self, job: RenderJob, frames: list[int]) -> None:
        job.blocks = self.create_pending_blocks(job, frames)

    def add_retry_blocks(self, job: RenderJob, frames: list[int], carry_attempt_count: int) -> list[RenderBlock]:
        new_blocks = self.create_pending_blocks(job, frames, carry_attempt_count=carry_attempt_count)
        job.blocks.extend(new_blocks)
        return new_blocks

    def next_pending_block(self, job: RenderJob) -> RenderBlock | None:
        pending_blocks = [block for block in job.blocks if block.status == "pending"]
        pending_blocks.sort(key=lambda item: (item.startFrame, item.endFrame, item.blockId))
        return pending_blocks[0] if pending_blocks else None

    def claim_pending_block(
        self,
        job: RenderJob,
        block: RenderBlock,
        frame_count: int = 1,
    ) -> RenderBlock:
        """Split a pending block so dispatch can happen frame by frame.

        This keeps the queue fair for heterogeneous workers while preserving
        the existing logical block grouping for retries and persistence.
        """
        if block.status != "pending":
            return block
        if frame_count <= 0:
            raise ValueError("frame_count must be greater than zero.")

        frames = block.frames()
        if len(frames) <= frame_count:
            return block

        attempt_count = block.attemptCount
        claim_start = frames[0]
        claim_end = frames[frame_count - 1]
        remaining_frames = frames[frame_count:]

        job.blocks.remove(block)

        claimed_block = RenderBlock(
            blockId=self._new_block_id(job, claim_start, claim_end),
            startFrame=claim_start,
            endFrame=claim_end,
            status="pending",
            attemptCount=attempt_count,
        )
        job.blocks.append(claimed_block)

        remainder_blocks = self.create_pending_blocks(
            job,
            remaining_frames,
            carry_attempt_count=attempt_count,
        )
        job.blocks.extend(remainder_blocks)
        return claimed_block

    def block_is_complete(self, block: RenderBlock, completed_frames: set[int]) -> bool:
        return all(frame in completed_frames for frame in block.frames())

    def missing_frames(self, block: RenderBlock, completed_frames: set[int]) -> list[int]:
        return [frame for frame in block.frames() if frame not in completed_frames]
