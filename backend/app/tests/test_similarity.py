import asyncio

import numpy as np

from app.services.embedding_service import embed_single_text

PAIRS = [
    ("Find the names of all the plans we offer", "List the names of all our plans"),
    ("How many plans do we have in total?", "What is the total number of plans?"),
    ("Find the names of all the plans we offer", "What is the average price of all plans?"),
    ("Find the names of all the plans we offer", "What is the meaning of life?"),
    ("List plans with a price greater than 500", "List plans with a price greater than 900"),
    ("How many plans do we have?", "How many offers do we have?"),
]


async def main() -> None:
    for a, b in PAIRS:
        va, vb = await embed_single_text(a), await embed_single_text(b)
        print(f"{float(np.dot(va, vb)):.3f}  |  {a}  <->  {b}")


if __name__ == "__main__":
    asyncio.run(main())
