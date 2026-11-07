import numpy as np


class Encoder:
    def __init__(self, vector=(1., 0.)):
        self.vector, self.queries = np.array(vector), []

    def query_embed(self, query):
        self.queries.append(query)
        return iter([self.vector.copy()])

    def passage_embed(self, texts, batch_size):
        return (self.vector.copy() for _ in texts)
