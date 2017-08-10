import string
from collections import defaultdict
from typing import List

import numpy as np
from scipy.stats import spearmanr

from data_processing.qa_data import ParagraphAndQuestion
from data_processing.span_data import compute_span_f1, get_best_span_bounded
from evaluator import Evaluation, Evaluator, record_span_predictions
from model import ModelOutput
from trivia_qa.trivia_qa_eval import f1_score as trivia_f1_score, exact_match_score as trivia_em_score
from utils import flatten_iterable


def get_best_text(word_start_probs, word_end_probs, bound, text):
    skip = {"a", "an", "the", ""}
    strip = string.punctuation + "".join([u"‘", u"’", u"´", u"`", "_"])
    scores = defaultdict(float)
    text = [x.lower() for x in text]
    text = ["" if x in skip else x.strip(strip) for x in text]

    for start_ix in range(0, len(word_start_probs)):
        prob = word_start_probs[start_ix]
        for end_ix in range(start_ix, min(start_ix + bound, len(word_end_probs))):
            pred = tuple(x for x in text[start_ix:end_ix] if len(x) > 0)
            if len(pred) > 0:
                scores[pred] += prob * word_end_probs[end_ix]

    best_score = -1
    best_word = None
    for word, score in scores.items():
        if score > best_score:
            best_score = score
            best_word = word
    return best_word, best_score


def trivia_span_scores(data: List[ParagraphAndQuestion],
                       prediction):
    scores = np.zeros((len(data), 4))
    for i in range(len(data)):
        para = data[i]
        ans = para.answer

        pred_span = prediction[i]
        pred_text = " ".join(flatten_iterable(para.context)[pred_span[0]:pred_span[1]+1])

        span_correct = False
        span_max_f1 = 0
        text_correct = 0
        text_max_f1 = 0

        for word_start, word_end in ans.answer_spans:
            answer_span = (word_start, word_end)
            span_max_f1 = max(span_max_f1, compute_span_f1(answer_span, pred_span))
            if answer_span == tuple(pred_span):
                span_correct = True

        for text in ans.answer_aliases:
            f1 = trivia_f1_score(pred_text, text)
            correct = trivia_em_score(pred_text, text)
            text_correct = max(text_correct, correct)
            text_max_f1 = max(text_max_f1, f1)

        scores[i] = [span_correct, span_max_f1, text_correct, text_max_f1]
    return scores


def trivia_span_evaluation(data: List[ParagraphAndQuestion],
                           true_len: int, prediction, prefix=""):
    scores = trivia_span_scores(data, prediction).sum(axis=0) / true_len
    return Evaluation({
        prefix + "accuracy": scores[0],
        prefix + "f1": scores[1],
        prefix + "text-accuracy": scores[2],
        prefix + "text-f1": scores[3]
    })


class TriviaQaBoundedSpanEvaluator(Evaluator):
    def __init__(self, bound: List[int], record_samples=False, tf_best_span=True):
        self.record_samples = record_samples
        self.bound = bound
        self.tf_best_span = tf_best_span

    def tensors_needed(self, model):
        return dict(p1=model.prediction.start_probs, p2=model.prediction.end_probs)

    def evaluate(self, data: List[ParagraphAndQuestion], true_len, **kargs):
        ev = Evaluation({})
        for b in self.bound:
            if "p1" in kargs:
                p1, p2 = kargs["p1"], kargs["p2"]
                best_spans = [get_best_span_bounded(p1[i], p2[i], b) for i in range(len(p1))]
                with_answers = [i for i in range(len(data)) if data[i].answer is not None]
                ev.add(trivia_span_evaluation([data[i] for i in with_answers],
                                              true_len, [best_spans[i][0] for i in with_answers], "b%d/" % b))
                if self.record_samples:
                    ev.add(record_span_predictions(best_spans, "bound-%d-span-predictions" % b))
            else:
                scores, spans = kargs["score"], kargs["span"]
                with_answers = [i for i in range(len(data)) if data[i].answer is not None]
                ev.add(trivia_span_evaluation([data[i] for i in with_answers],
                                              true_len, [spans[i] for i in with_answers], "b%d/" % b))
        return ev


class TfTriviaQaBoundedSpanEvaluator(Evaluator):
    """ Computes the best span in tensorflow, meaning which we expect to be faster and
    does not require us having to keep the entire set of output logits in RAM """
    def __init__(self, bound: List[int]):
        self.bound = bound

    def tensors_needed(self, model):
        needed = {}
        for b in self.bound:
            span, _ = model.prediction.get_best_span(b)
            needed.update({("span_%d" % b): span})
        return needed

    def evaluate(self, data: List[ParagraphAndQuestion], true_len, **kargs):
        ev = Evaluation({})
        for b in self.bound:
            spans = kargs[("span_%d" % b)]
            with_answer = [i for i,x in enumerate(data) if len(x.answer.answer_spans) > 0]
            ev.add(trivia_span_evaluation([data[i] for i in with_answer], true_len,
                                          [spans[i] for i in with_answer], "b%d/" % b))
        return ev


class ConfidenceEvaluator(Evaluator):
    def __init__(self, bound: int):
        self.bound = bound

    def tensors_needed(self, model):
        spans, conf = model.prediction.get_best_span(self.bound)
        return dict(none_conf=model.prediction.none_prob,
                    spans=spans,
                    conf=conf)

    def evaluate(self, data: List[ParagraphAndQuestion], true_len, **kargs):
        scores = trivia_span_scores(data, kargs["spans"])
        none_conf = kargs["none_conf"]
        aggregated_scores = scores.sum(axis=0) / true_len
        prefix ="b%d/" % self.bound
        ev = Evaluation({
            prefix + "accuracy": aggregated_scores[0],
            prefix + "f1": aggregated_scores[1],
            prefix + "text-accuracy": aggregated_scores[2],
            prefix + "text-f1": aggregated_scores[3],
            prefix + "text-f1-spearman-r": spearmanr(none_conf, scores[:, 3])[0],
            prefix + "span-accuracy-spearman-r": spearmanr(none_conf, scores[:, 0])[0]
        })
        print(ev.scalars)
        return ev


class TriviaqaAggregatedTextEvaluator(Evaluator):
    """ Accuracy when the span probabilities are aggregated across all spans with the
    same normalized text """
    def __init__(self, bound: int):
        self.bound = bound

    def tensors_needed(self, prediction: ModelOutput):
        return dict(start=prediction.prediction.start_probs, end=prediction.prediction.end_probs)

    def evaluate(self, input: List, true_len, **kwargs) -> Evaluation:
        start = kwargs["start"]
        end = kwargs["end"]
        total_f1 = 0
        total_correct = 0

        for ix, doc in enumerate(input):
            text_correct = 0
            text_max_f1 = 0
            pred_text = " ".join(get_best_text(start[ix], end[ix], self.bound, flatten_iterable(doc.context))[0])

            for text in doc.answer.answer_aliases:
                f1 = trivia_f1_score(pred_text, text)
                correct = trivia_em_score(pred_text, text)
                text_correct = max(text_correct, correct)
                text_max_f1 = max(text_max_f1, f1)
            total_f1 += text_max_f1
            total_correct += correct

        return Evaluation({"agg-text-f1": total_f1/true_len, "agg-text-em": total_correct/true_len})