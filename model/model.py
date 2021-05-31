"""
Model defintion
"""

import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
# from IPython import display
from utils import clone_variable_list, create_fc_layer, create_conv_layer
from utils.resnet_utils import _conv, _fc, _bn, _residual_block, _residual_block_first
from utils.vgg_utils import vgg_conv_layer, vgg_fc_layer
import math

PARAM_XI_STEP = 1e-3
NEG_INF = -1e32
EPSILON = 1e-32
HYBRID_ALPHA = 0.5
TRAIN_ENTROPY_BASED_SUM = False


def weight_variable(shape, name='fc', init_type='default'):
    """
    Define weight variables
    Args:
        shape       Shape of the bias variable tensor

    Returns:
        A tensor of size shape initialized from a random normal
    """
    with tf.variable_scope(name):
        if init_type == 'default':
            weights = tf.get_variable('weights', shape, tf.float32,
                                      initializer=tf.truncated_normal_initializer(stddev=0.1))
            # weights = tf.Variable(tf.truncated_normal(shape, stddev=0.1), name='weights')
        elif init_type == 'zero':
            weights = tf.get_variable('weights', shape, tf.float32, initializer=tf.constant_initializer(0.1))
            # weights = tf.Variable(tf.constant(0.1, shape=shape, dtype=np.float32), name='weights')

    return weights


def bias_variable(shape, name='fc'):
    """
    Define bias variables
    Args:
        shape       Shape of the bias variable tensor

    Returns:
        A tensor of size shape initialized from a constant
    """
    with tf.variable_scope(name):
        biases = tf.get_variable('biases', shape, initializer=tf.constant_initializer(0.1))

    return biases
    # return tf.Variable(tf.constant(0.1, shape=shape, dtype=np.float32), name='biases') #TODO: Should we initialize it from 0


def masked_softmax(scores, mask):
    with tf.name_scope('masked_softmax'):
        scores = scores - tf.reduce_max(scores, axis=(-1,), keep_dims=True)
        exp_scores = tf.exp(scores)
        exp_scores *= mask
        exp_sum_scores = tf.reduce_sum(exp_scores, axis=-1, keep_dims=True)
    return exp_scores / exp_sum_scores


class Model:
    """
    A class defining the model
    """

    def __init__(self, x_train, y_, num_tasks, opt, imp_method, synap_stgth, fisher_update_after, fisher_ema_decay, m1,
                 m2, s, network_arch='FC-S',
                 is_ATT_DATASET=False, x_test=None, attr=None):
        """
        Instantiate the model
        """
        # Define some placeholders which are used to feed the data to the model
        self.y_ = y_
        if imp_method == 'PNN':
            self.train_phase = []
            self.total_classes = int(self.y_[0].get_shape()[1])
            self.train_phase = [tf.placeholder(tf.bool, name='train_phase_%d' % (i)) for i in range(num_tasks)]
            self.output_mask = [tf.placeholder(dtype=tf.float32, shape=[self.total_classes]) for i in range(num_tasks)]
        else:
            self.total_classes = int(self.y_.get_shape()[1])
            self.train_phase = tf.placeholder(tf.bool, name='train_phase')
            if (
                    imp_method == 'A-GEM' or imp_method == 'ER' or imp_method == 'MEGA' or imp_method == 'MEGAD' or imp_method == 'MEGA_RA' or imp_method == 'AKCL') and 'FC-' not in network_arch:  # Only for Split-X setups
                self.output_mask = [tf.placeholder(dtype=tf.float32, shape=[self.total_classes]) for i in
                                    range(num_tasks)]
                self.mem_batch_size = tf.placeholder(dtype=tf.float32, shape=())
            else:
                self.output_mask = tf.placeholder(dtype=tf.float32, shape=[self.total_classes])
        self.sample_weights = tf.placeholder(tf.float32, shape=[None])
        self.task_id = tf.placeholder(dtype=tf.int32, shape=())
        self.store_grad_batches = tf.placeholder(dtype=tf.float32, shape=())
        self.keep_prob = tf.placeholder(dtype=tf.float32, shape=())
        self.train_samples = tf.placeholder(dtype=tf.float32, shape=())
        self.training_iters = tf.placeholder(dtype=tf.float32, shape=())
        self.train_step = tf.placeholder(dtype=tf.float32, shape=())
        self.violation_count = tf.Variable(0, dtype=tf.float32, trainable=False)
        self.is_ATT_DATASET = is_ATT_DATASET  # To use a different (standard one) ResNet-18 for CUB

        self.flag1 = tf.placeholder(dtype=tf.int32, shape=(), name='flag1')

        if imp_method == 'AKCL':
            # print(self.total_classes)
            # exit()
            self.m1 = m1
            self.m2 = m2
            self.s = s
            self.current_task_id = tf.placeholder(dtype=tf.int32, shape=(), name='Current_task')
            self.previous_task_id = tf.placeholder(dtype=tf.int32, shape=(), name='Previous_task')
            self.reset_storage = tf.placeholder(dtype=tf.bool, shape=(), name='reset_storage')
            self.store_loss = tf.placeholder(dtype=tf.float32, shape=[None], name='store_del_ref_loss')
            if 'FC-' in network_arch:
                self.store_grads = tf.placeholder(dtype=tf.float32, shape=[None, 269312], name='store_grads')  # 269312
                self.store_kl_grads = tf.placeholder(dtype=tf.float32, shape=[None, 269312], name='stored_kl_grads')
                self.org_feat = tf.placeholder(tf.float32, shape=[None, 256], name='org_features')
            elif network_arch == 'RESNET-S':
                self.store_grads = tf.placeholder(dtype=tf.float32, shape=[None, 1109140], name='store_grads')
                self.store_kl_grads = tf.placeholder(dtype=tf.float32, shape=[None, 1109140], name='stored_kl_grads')
                self.org_feat = tf.placeholder(tf.float32, shape=[None, 160], name='org_features')
            elif network_arch == 'RESNET-B' and self.total_classes == 200:
                self.store_grads = tf.placeholder(dtype=tf.float32, shape=[None, 11277120], name='store_grads')
                self.store_kl_grads = tf.placeholder(dtype=tf.float32, shape=[None, 11277120], name='stored_kl_grads')
                self.org_feat = tf.placeholder(tf.float32, shape=[None, 512], name='org_features')
            elif network_arch == 'RESNET-B' and self.total_classes == 850:
                self.store_grads = tf.placeholder(dtype=tf.float32, shape=[None, 11609920], name='store_grads')
                self.store_kl_grads = tf.placeholder(dtype=tf.float32, shape=[None, 11609920], name='stored_kl_grads')
                self.org_feat = tf.placeholder(tf.float32, shape=[None, 512], name='org_features')

        # self.current_task_id = tf.placeholder(dtype=tf.int32, shape=(), name='Current_task')
        # self.is_aug = tf.placeholder(dtype=tf.bool, name='is_aug')

        # self.last_ref_loss = tf.placeholder(dtype=tf.float32, shape=(), name='last_ref_loss') 
        # self.last_cur_loss = tf.placeholder(dtype=tf.float32, shape=(), name='last_cur_loss') 

        if x_test is not None:
            # If CUB datatset then use augmented x (x_train) for training and non-augmented x (x_test) for testing
            self.x = tf.cond(self.train_phase, lambda: tf.identity(x_train), lambda: tf.identity(x_test))
            train_shape = x_train.get_shape().as_list()
            x = tf.reshape(self.x, [-1, train_shape[1], train_shape[2], train_shape[3]])
        else:
            # We don't use data augmentation for other datasets
            self.x = x_train
            x = self.x

        # Class attributes for zero shot transfer
        self.class_attr = attr

        if self.class_attr is not None:
            self.attr_dims = int(self.class_attr.get_shape()[1])

        # Save the arguments passed from the main script
        self.opt = opt
        self.num_tasks = num_tasks
        self.imp_method = imp_method
        self.fisher_update_after = fisher_update_after
        self.fisher_ema_decay = fisher_ema_decay
        self.network_arch = network_arch

        # A scalar variable for previous syanpse strength
        self.synap_stgth = tf.constant(synap_stgth, shape=[1], dtype=tf.float32)
        self.triplet_loss_scale = 2.1

        # Define different variables
        self.weights_old = []
        self.star_vars = []
        self.small_omega_vars = []
        self.big_omega_vars = []
        self.big_omega_riemann_vars = []
        self.fisher_diagonal_at_minima = []
        self.hebbian_score_vars = []
        self.running_fisher_vars = []
        self.tmp_fisher_vars = []
        self.max_fisher_vars = []
        self.min_fisher_vars = []
        self.max_score_vars = []
        self.min_score_vars = []
        self.normalized_score_vars = []
        self.score_vars = []
        self.normalized_fisher_at_minima_vars = []
        self.weights_delta_old_vars = []
        self.ref_grads = []

        self.projected_gradients_list = []

        if self.class_attr is not None:
            self.loss_and_train_ops_for_attr_vector(x, self.y_)
        else:
            self.loss_and_train_ops_for_one_hot_vector(x, self.y_)

        # Set the operations to reset the optimier when needed
        self.reset_optimizer_ops()

    ####################################################################################
    #### Internal APIs of the class. These should not be called/ exposed externally ####
    ####################################################################################
    def loss_and_train_ops_for_one_hot_vector(self, x, y_):
        """
        Loss and training operations for the training of one-hot vector based classification model
        """
        # Define approproate network
        if self.network_arch == 'FC-S':
            input_dim = int(x.get_shape()[1])
            layer_dims = [input_dim, 256, 256, self.total_classes]
            if self.imp_method == 'PNN':
                self.task_logits = []
                self.task_pruned_logits = []
                self.unweighted_entropy = []
                for i in range(self.num_tasks):
                    if i == 0:
                        self.task_logits.append(self.init_fc_column_progNN(layer_dims, x))
                        self.task_pruned_logits.append(tf.where(
                            tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(self.task_logits[i])[0], 1]),
                            self.task_logits[i], NEG_INF * tf.ones_like(self.task_logits[i])))
                        self.unweighted_entropy.append(tf.squeeze(tf.reduce_mean(
                            tf.nn.softmax_cross_entropy_with_logits(labels=y_[i], logits=self.task_pruned_logits[
                                i]))))  # mult by mean(y_[i]) puts unwaranted loss to 0
                    else:
                        self.task_logits.append(self.extensible_fc_column_progNN(layer_dims, x, i))
                        self.task_pruned_logits.append(tf.where(
                            tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(self.task_logits[i])[0], 1]),
                            self.task_logits[i], NEG_INF * tf.ones_like(self.task_logits[i])))
                        self.unweighted_entropy.append(tf.squeeze(tf.reduce_mean(
                            tf.nn.softmax_cross_entropy_with_logits(labels=y_[i], logits=self.task_pruned_logits[
                                i]))))  # mult by mean(y_[i]) puts unwaranted loss to 0
            else:
                self.fc_variables(layer_dims)
                logits = self.fc_feedforward(x, self.weights, self.biases)

        elif self.network_arch == 'FC-B':
            input_dim = int(x.get_shape()[1])
            layer_dims = [input_dim, 2000, 2000, self.total_classes]
            self.fc_variables(layer_dims)
            logits = self.fc_feedforward(x, self.weights, self.biases)

        elif self.network_arch == 'CNN':
            num_channels = int(x.get_shape()[-1])
            self.image_size = int(x.get_shape()[1])
            kernels = [3, 3, 3, 3, 3]
            depth = [num_channels, 32, 32, 64, 64, 512]
            self.conv_variables(kernels, depth)
            logits = self.conv_feedforward(x, self.weights, self.biases, apply_dropout=True)

        elif self.network_arch == 'VGG':
            # VGG-16
            logits = self.vgg_16_conv_feedforward(x)

        elif 'RESNET-' in self.network_arch:
            if self.network_arch == 'RESNET-S':
                # Same resnet-18 as used in GEM paper
                kernels = [3, 3, 3, 3, 3]
                filters = [20, 20, 40, 80, 160]
                strides = [1, 0, 2, 2, 2]
            elif self.network_arch == 'RESNET-B':
                # Standard ResNet-18
                kernels = [7, 3, 3, 3, 3]
                filters = [64, 64, 128, 256, 512]
                strides = [2, 0, 2, 2, 2]
            if self.imp_method == 'PNN':
                self.task_logits = []
                self.task_pruned_logits = []
                self.unweighted_entropy = []
                for i in range(self.num_tasks):
                    if i == 0:
                        self.task_logits.append(self.init_resent_column_progNN(x, kernels, filters, strides))
                    else:
                        self.task_logits.append(self.extensible_resnet_column_progNN(x, kernels, filters, strides, i))
                    self.task_pruned_logits.append(tf.where(
                        tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(self.task_logits[i])[0], 1]),
                        self.task_logits[i], NEG_INF * tf.ones_like(self.task_logits[i])))
                    self.unweighted_entropy.append(tf.squeeze(tf.reduce_mean(
                        tf.nn.softmax_cross_entropy_with_logits(labels=y_[i], logits=self.task_pruned_logits[i]))))
            elif self.imp_method == 'A-GEM' or self.imp_method == 'ER' or self.imp_method == 'MEGA' or self.imp_method == 'MEGAD' or self.imp_method == 'MEGA_RA':
                logits = self.resnet18_conv_feedforward(x, kernels, filters, strides)
                self.task_pruned_logits = []
                self.unweighted_entropy = []
                for i in range(self.num_tasks):
                    self.task_pruned_logits.append(
                        tf.where(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(logits)[0], 1]), logits,
                                 NEG_INF * tf.ones_like(logits)))
                    cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=y_,
                                                                            logits=self.task_pruned_logits[i])
                    adjusted_entropy = tf.reduce_sum(
                        tf.cast(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(y_)[0], 1]),
                                dtype=tf.float32) * y_, axis=1) * cross_entropy
                    self.unweighted_entropy.append(tf.reduce_sum(adjusted_entropy))  # We will average it later on
                    # self.KL_loss = tf.cond(tf.equal(self.flag1,0), lambda:0.0, lambda:0.0000001*tf.reduce_mean(tf.abs(self.org_feat-self.features)))
            elif self.imp_method == 'AKCL':
                logits = self.resnet18_conv_feedforward(x, kernels, filters, strides)  #
                self.task_pruned_logits = []
                self.unweighted_entropy = []
                self.theta = []
                print('aaaaaaaaa')
                for i in range(self.num_tasks):
                    task_selected = np.zeros(self.output_mask[i][None, :].shape)
                    for k in range(i + 1):
                        task_selected += self.output_mask[k][None, :]
                    self.task_pruned_logits.append(
                        tf.where(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(logits)[0], 1]), logits,
                                 tf.zeros_like(logits)))
                    # logits: all predicted
                    # y_: the ground truth
                    # task_pruned_logits: the related task                

                    # noraml softmax
                    # cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=self.task_pruned_logits[i])

                    # single margin softmax: only m2 
                    # m2 = 0.01
                    # intraTaskTheta = tf.acos(self.task_pruned_logits[i]) # (10, 100)
                    # marginal_logits_2 = tf.cos(intraTaskTheta + m2) # (10, 100)
                    # final_logits = self.task_pruned_logits[i] + tf.where(tf.equal(y_, 1.0), marginal_logits_2 - self.task_pruned_logits[i], tf.zeros_like(logits))
                    # cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=24*final_logits)

                    # Double margin softmax
                    m1 = 0.4
                    m2 = 0.01
                    # s CIFAR: 24; CUB: 20; AWA
                    s = 20
                    m1 = self.m1
                    m2 = self.m2
                    s = self.s
                    selected_logits = tf.where(tf.tile(tf.equal(task_selected, 1.0), [tf.shape(logits)[0], 1]), logits,
                                               tf.zeros_like(logits))
                    # # 1. For each related task logits, we have a larger m2
                    theta = tf.acos(selected_logits)  # (10, 100)
                    self.theta.append(theta)
                    marginal_logits = tf.cos(theta + m1)  # (10, 100)
                    marginal_logits_2 = tf.cos(theta + m2)  # (10, 100)
                    final_logits = selected_logits + \
                                   tf.where(tf.tile(tf.equal(task_selected, 1.0), [tf.shape(marginal_logits)[0], 1]),
                                            marginal_logits - selected_logits, tf.zeros_like(logits)) + \
                                   tf.where(tf.equal(y_, 1.0), marginal_logits_2 - selected_logits,
                                            tf.zeros_like(logits))
                    cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=s * final_logits)

                    adjusted_entropy = tf.reduce_sum(
                        tf.cast(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(y_)[0], 1]),
                                dtype=tf.float32) * y_, axis=1) * cross_entropy
                    self.unweighted_entropy.append(tf.reduce_sum(adjusted_entropy))  # We will average it later on
                    self.KL_loss = tf.cond(tf.equal(self.flag1, 0), lambda: 0.0,
                                           lambda: tf.reduce_mean(tf.abs(self.org_feat - self.features)))

            else:
                logits = self.resnet18_conv_feedforward(x, kernels, filters, strides)

        # Prune the predictions to only include the classes for which
        # the training data is present
        if (self.imp_method != 'PNN') and ((
                                                   self.imp_method != 'A-GEM' and self.imp_method != 'ER' and self.imp_method != 'MEGA' and self.imp_method != 'MEGAD' and self.imp_method != 'MEGA_RA' and self.imp_method != 'AKCL') or 'FC-' in self.network_arch):
            self.pruned_logits = tf.where(tf.tile(tf.equal(self.output_mask[None, :], 1.0), [tf.shape(logits)[0], 1]),
                                          logits, NEG_INF * tf.ones_like(logits))
            # self.ws_pruned_logits = tf.where(tf.tile(tf.equal(self.output_mask[None, :], 1.0), [tf.shape(self.ws_log)[0], 1]),
            #                               self.ws_log, tf.zeros_like(self.ws_log))

        # Create list of variables for storing different measures
        # Note: This method has to be called before calculating fisher
        # or any other importance measure
        self.init_vars()

        # Different entropy measures/ loss definitions
        if (self.imp_method != 'PNN') and ((
                                                   self.imp_method != 'A-GEM' and self.imp_method != 'ER' and self.imp_method != 'MEGA' and self.imp_method != 'MEGAD' and self.imp_method != 'MEGA_RA' and self.imp_method != 'AKCL') or 'FC-' in self.network_arch):
            if self.imp_method == 'AKCL':
                self.mse = 2.0 * tf.nn.l2_loss(self.pruned_logits)  # tf.nn.l2_loss computes sum(T**2)/ 2
                self.weighted_entropy = tf.reduce_mean(tf.losses.softmax_cross_entropy(y_,
                                                                                       self.pruned_logits,
                                                                                       self.sample_weights,
                                                                                       reduction=tf.losses.Reduction.NONE))
                m2 = 0.001
                m2 = self.m2
                print(m2)
                s = 32
                theta = tf.acos(self.pruned_logits)
                self.thetaw = theta
                self.thetaww = (180 / 3.1415926) * tf.acos(
                    tf.reduce_sum(tf.where(tf.equal(y_, 1.0), self.pruned_logits, tf.zeros_like(self.pruned_logits)),
                                  -1))
                marginal_logits_2 = tf.cos(theta + m2)
                final_logits = self.pruned_logits + tf.where(tf.equal(y_, 1.0), marginal_logits_2 - self.pruned_logits,
                                                             tf.zeros_like(self.pruned_logits))
                self.unweighted_entropy = tf.reduce_mean(
                    tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=s * final_logits))
                self.KL_loss = tf.cond(tf.equal(self.flag1, 0), lambda: 0.0,
                                       lambda: tf.reduce_mean(tf.abs(self.org_feat - self.features)))

            else:
                self.mse = 2.0 * tf.nn.l2_loss(self.pruned_logits)  # tf.nn.l2_loss computes sum(T**2)/ 2
                self.weighted_entropy = tf.reduce_mean(tf.losses.softmax_cross_entropy(y_,
                                                                                       self.pruned_logits,
                                                                                       self.sample_weights,
                                                                                       reduction=tf.losses.Reduction.NONE))
                # theta = tf.acos(self.ws_pruned_logits)
                # self.thetaw = theta
                self.unweighted_entropy = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=y_,
                                                                                                 logits=self.pruned_logits))

        # Create operations for loss and gradient calculation
        self.loss_and_gradients(self.imp_method)

        if self.imp_method != 'PNN':
            # Store the current weights before doing a train step
            self.get_current_weights()

        # For GEM variants train ops will be defined later
        if 'GEM' not in self.imp_method and 'MEGA' not in self.imp_method and 'MEGAD' not in self.imp_method and 'AKCL' not in self.imp_method:
            # Define the training operation here as Pathint ops depend on the train ops
            self.train_op()

        # Create operations to compute importance depending on the importance methods
        if self.imp_method == 'EWC':
            self.create_fisher_ops()
        elif self.imp_method == 'M-EWC':
            self.create_fisher_ops()
            self.create_pathint_ops()
            self.combined_fisher_pathint_ops()
        elif self.imp_method == 'PI':
            self.create_pathint_ops()
        elif self.imp_method == 'RWALK':
            self.create_fisher_ops()
            self.create_pathint_ops()
        elif self.imp_method == 'MAS':
            self.create_hebbian_ops()
        elif self.imp_method == 'A-GEM' or self.imp_method == 'S-GEM':
            self.create_stochastic_gem_ops()
        elif self.imp_method == 'MEGA':
            self.create_stochastic_mega_ops()
        elif self.imp_method == 'MEGAD':
            self.create_stochastic_megad_ops()
        elif self.imp_method == 'MEGA_RA':
            self.create_stochastic_megara_ops()
        elif self.imp_method == 'AKCL':
            self.create_stochastic_akcl_ops()

        if self.imp_method != 'PNN':
            # Create weight save and store ops
            self.weights_store_ops()

            # Summary operations for visualization
            tf.summary.scalar("unweighted_entropy", self.unweighted_entropy)
            for v in self.trainable_vars:
                tf.summary.histogram(v.name.replace(":", "_"), v)
            self.merged_summary = tf.summary.merge_all()

        # Accuracy measure
        if (self.imp_method == 'PNN') or ((
                                                  self.imp_method == 'A-GEM' or self.imp_method == 'ER' or self.imp_method == 'MEGA' or self.imp_method == 'MEGAD' or self.imp_method == 'MEGA_RA' or self.imp_method == 'AKCL') and 'FC-' not in self.network_arch):
            self.correct_predictions = []
            self.accuracy = []
            for i in range(self.num_tasks):
                if self.imp_method == 'PNN':
                    self.correct_predictions.append(
                        tf.equal(tf.argmax(self.task_pruned_logits[i], 1), tf.argmax(y_[i], 1)))
                else:
                    self.correct_predictions.append(
                        tf.equal(tf.argmax(self.task_pruned_logits[i], 1), tf.argmax(y_, 1)))
                self.accuracy.append(tf.reduce_mean(tf.cast(self.correct_predictions[i], tf.float32)))
        else:
            self.correct_predictions = tf.equal(tf.argmax(self.pruned_logits, 1), tf.argmax(y_, 1))
            self.accuracy = tf.reduce_mean(tf.cast(self.correct_predictions, tf.float32))

    def loss_and_train_ops_for_attr_vector(self, x, y_):
        """
        Loss and training operations for the training of joined embedding model
        """
        # Define approproate network
        if self.network_arch == 'FC-S':
            input_dim = int(x.get_shape()[1])
            layer_dims = [input_dim, 256, 256, self.total_classes]
            self.fc_variables(layer_dims)
            logits = self.fc_feedforward(x, self.weights, self.biases)

        elif self.network_arch == 'FC-B':
            input_dim = int(x.get_shape()[1])
            layer_dims = [input_dim, 2000, 2000, self.total_classes]
            self.fc_variables(layer_dims)
            logits = self.fc_feedforward(x, self.weights, self.biases)

        elif self.network_arch == 'CNN':
            num_channels = int(x.get_shape()[-1])
            self.image_size = int(x.get_shape()[1])
            kernels = [3, 3, 3, 3, 3]
            depth = [num_channels, 32, 32, 64, 64, 512]
            self.conv_variables(kernels, depth)
            logits = self.conv_feedforward(x, self.weights, self.biases, apply_dropout=True)

        elif self.network_arch == 'VGG':
            # VGG-16
            phi_x = self.vgg_16_conv_feedforward(x)

        elif self.network_arch == 'RESNET-S':
            # Standard ResNet-18
            kernels = [3, 3, 3, 3, 3]
            filters = [20, 20, 40, 80, 160]
            strides = [1, 0, 2, 2, 2]
            # Get the image features
            phi_x = self.resnet18_conv_feedforward(x, kernels, filters, strides)

        elif self.network_arch == 'RESNET-B':
            # Standard ResNet-18
            kernels = [7, 3, 3, 3, 3]
            filters = [64, 64, 128, 256, 512]
            strides = [2, 0, 2, 2, 2]
            # Get the image features
            phi_x = self.resnet18_conv_feedforward(x, kernels, filters, strides)

        # Get the attributes embedding
        attr_embed = self.get_attribute_embedding(
            self.class_attr)  # Does not contain biases yet, Dimension: TOTAL_CLASSES x image_feature_dim
        # Add the biases now
        last_layer_biases = bias_variable([self.total_classes], name='attr_embed_b')
        self.trainable_vars.append(last_layer_biases)

        # Now that we have all the trainable variables, initialize the different book keeping variables
        # Note: This method has to be called before calculating fisher
        # or any other importance measure
        self.init_vars()

        # Compute the logits for the ZST case
        zst_logits = tf.matmul(phi_x, tf.transpose(attr_embed)) + last_layer_biases
        # Prune the predictions to only include the classes for which
        # the training data is present
        if self.imp_method == 'A-GEM':
            pruned_zst_logits = []
            self.unweighted_entropy = []
            for i in range(self.num_tasks):
                pruned_zst_logits.append(
                    tf.where(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(zst_logits)[0], 1]),
                             zst_logits, NEG_INF * tf.ones_like(zst_logits)))
                cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=pruned_zst_logits[i])
                adjusted_entropy = tf.reduce_sum(
                    tf.cast(tf.tile(tf.equal(self.output_mask[i][None, :], 1.0), [tf.shape(y_)[0], 1]),
                            dtype=tf.float32) * y_, axis=1) * cross_entropy
                self.unweighted_entropy.append(tf.reduce_sum(adjusted_entropy))
        else:
            pruned_zst_logits = tf.where(tf.tile(tf.equal(self.output_mask[None, :], 1.0),
                                                 [tf.shape(zst_logits)[0], 1]), zst_logits,
                                         NEG_INF * tf.ones_like(zst_logits))
            self.unweighted_entropy = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits(labels=y_, logits=pruned_zst_logits))
            self.mse = 2.0 * tf.nn.l2_loss(pruned_zst_logits)  # tf.nn.l2_loss computes sum(T**2)/ 2

        # Create operations for loss and gradient calculation
        self.loss_and_gradients(self.imp_method)

        # Store the current weights before doing a train step
        self.get_current_weights()

        if 'GEM' not in self.imp_method:
            self.train_op()

        # Create operations to compute importance depending on the importance methods
        if self.imp_method == 'EWC':
            self.create_fisher_ops()
        elif self.imp_method == 'M-EWC':
            self.create_fisher_ops()
            self.create_pathint_ops()
            self.combined_fisher_pathint_ops()
        elif self.imp_method == 'PI':
            self.create_pathint_ops()
        elif self.imp_method == 'RWALK':
            self.create_fisher_ops()
            self.create_pathint_ops()
        elif self.imp_method == 'MAS':
            self.create_hebbian_ops()
        elif (self.imp_method == 'A-GEM') or (self.imp_method == 'S-GEM'):
            self.create_stochastic_gem_ops()

        # Create weight save and store ops
        self.weights_store_ops()

        # Summary operations for visualization
        tf.summary.scalar("triplet_loss", self.unweighted_entropy)
        for v in self.trainable_vars:
            tf.summary.histogram(v.name.replace(":", "_"), v)
        self.merged_summary = tf.summary.merge_all()

        # Accuracy measure
        if self.imp_method == 'A-GEM' and 'FC-' not in self.network_arch:
            self.correct_predictions = []
            self.accuracy = []
            for i in range(self.num_tasks):
                self.correct_predictions.append(tf.equal(tf.argmax(pruned_zst_logits[i], 1), tf.argmax(y_, 1)))
                self.accuracy.append(tf.reduce_mean(tf.cast(self.correct_predictions[i], tf.float32)))
        else:
            self.correct_predictions = tf.equal(tf.argmax(pruned_zst_logits, 1), tf.argmax(y_, 1))
            self.accuracy = tf.reduce_mean(tf.cast(self.correct_predictions, tf.float32))

    def init_fc_column_progNN(self, layer_dims, h, apply_dropout=False):
        """
        Defines the first column of Progressive NN - FC Networks
        """
        self.trainable_vars = []
        self.h_pnn = []

        self.trainable_vars.append([])
        self.h_pnn.append([])
        self.h_pnn[0].append(h)
        for i in range(len(layer_dims) - 1):
            w = weight_variable([layer_dims[i], layer_dims[i + 1]], name='fc_w_%d_t0' % (i))
            b = bias_variable([layer_dims[i + 1]], name='fc_b_%d_t0' % (i))
            self.trainable_vars[0].append(w)
            self.trainable_vars[0].append(b)
            if i == len(layer_dims) - 2:
                # Last layer (logits) - don't apply the relu
                h = create_fc_layer(h, w, b, apply_relu=False)
            else:
                h = create_fc_layer(h, w, b)
                if apply_dropout:
                    h = tf.nn.dropout(h, 1)
            self.h_pnn[0].append(h)

        return h

    def extensible_fc_column_progNN(self, layer_dims, h, task, apply_dropout=False):
        """
        Define the subsequent columns of the progressive NN - FC Networks
        """
        self.trainable_vars.append([])
        self.h_pnn.append([])
        self.h_pnn[task].append(h)
        for i in range(len(layer_dims) - 1):
            w = weight_variable([layer_dims[i], layer_dims[i + 1]], name='fc_w_%d_t%d' % (i, task))
            b = bias_variable([layer_dims[i + 1]], name='fc_b_%d_t%d' % (i, task))
            self.trainable_vars[task].append(w)
            self.trainable_vars[task].append(b)
            preactivation = create_fc_layer(h, w, b, apply_relu=False)
            for tt in range(task):
                U_w = weight_variable([layer_dims[i], layer_dims[i + 1]], name='fc_uw_%d_t%d_tt%d' % (i, task, tt))
                U_b = bias_variable([layer_dims[i + 1]], name='fc_ub_%d_t%d_tt%d' % (i, task, tt))
                self.trainable_vars[task].append(U_w)
                self.trainable_vars[task].append(U_b)
                preactivation += create_fc_layer(self.h_pnn[tt][i], U_w, U_b, apply_relu=False)
            if i == len(layer_dims) - 2:
                # Last layer (logits) - don't apply the relu
                h = preactivation
            else:
                # layer < last layer, apply relu
                h = tf.nn.relu(preactivation)
                if apply_dropout:
                    h = tf.nn.dropout(h)
            self.h_pnn[task].append(h)

        return h

    def init_resent_column_progNN(self, x, kernels, filters, strides):
        """
        Defines the first column of Progressive NN - ResNet-18
        """
        self.trainable_vars = []
        self.h_pnn = []

        self.trainable_vars.append([])
        self.h_pnn.append([])
        self.h_pnn[0].append(x)

        # Conv1
        h = _conv(x, kernels[0], filters[0], strides[0], self.trainable_vars[0], name='conv_1_t0')
        h = _bn(h, self.trainable_vars[0], self.train_phase[0], name='bn_1_t0')
        h = tf.nn.relu(h)
        self.h_pnn[0].append(h)

        # Conv2_x
        h = _residual_block(h, self.trainable_vars[0], self.train_phase[0], name='conv2_1_t0')
        h = _residual_block(h, self.trainable_vars[0], self.train_phase[0], name='conv2_2_t0')
        self.h_pnn[0].append(h)

        # Conv3_x
        h = _residual_block_first(h, filters[2], strides[2], self.trainable_vars[0], self.train_phase[0],
                                  name='conv3_1_t0', is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[0], self.train_phase[0], name='conv3_2_t0')
        self.h_pnn[0].append(h)

        # Conv4_x
        h = _residual_block_first(h, filters[3], strides[3], self.trainable_vars[0], self.train_phase[0],
                                  name='conv4_1_t0', is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[0], self.train_phase[0], name='conv4_2_t0')
        self.h_pnn[0].append(h)

        # Conv5_x
        h = _residual_block_first(h, filters[4], strides[4], self.trainable_vars[0], self.train_phase[0],
                                  name='conv5_1_t0', is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[0], self.train_phase[0], name='conv5_2_t0')
        self.h_pnn[0].append(h)

        # Apply average pooling
        h = tf.reduce_mean(h, [1, 2])

        if self.network_arch == 'RESNET-S':
            logits = _fc(h, self.total_classes, self.trainable_vars[0], name='fc_1_t0', is_cifar=True)
        else:
            logits = _fc(h, self.total_classes, self.trainable_vars[0], name='fc_1_t0')
        self.h_pnn[0].append(logits)

        return logits

    def extensible_resnet_column_progNN(self, x, kernels, filters, strides, task):
        """
        Define the subsequent columns of the progressive NN - ResNet-18
        """
        self.trainable_vars.append([])
        self.h_pnn.append([])
        self.h_pnn[task].append(x)

        # Conv1
        h = _conv(x, kernels[0], filters[0], strides[0], self.trainable_vars[task], name='conv_1_t%d' % (task))
        h = _bn(h, self.trainable_vars[task], self.train_phase[task], name='bn_1_t%d' % (task))
        # Add lateral connections
        for tt in range(task):
            U_w = weight_variable([1, 1, self.h_pnn[tt][0].get_shape().as_list()[-1], h.get_shape().as_list()[-1]],
                                  name='conv_1_w_t%d_tt%d' % (task, tt))
            U_b = bias_variable([h.get_shape().as_list()[-1]], name='conv_1_b_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            h += create_conv_layer(self.h_pnn[tt][0], U_w, U_b, apply_relu=False)
        h = tf.nn.relu(h)
        self.h_pnn[task].append(h)

        # Conv2_x
        h = _residual_block(h, self.trainable_vars[task], self.train_phase[task], name='conv2_1_t%d' % (task))
        h = _residual_block(h, self.trainable_vars[task], self.train_phase[task], apply_relu=False,
                            name='conv2_2_t%d' % (task))
        # Add lateral connections
        for tt in range(task):
            U_w = weight_variable([1, 1, self.h_pnn[tt][1].get_shape().as_list()[-1], h.get_shape().as_list()[-1]],
                                  name='conv_2_w_t%d_tt%d' % (task, tt))
            U_b = bias_variable([h.get_shape().as_list()[-1]], name='conv_2_b_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            h += create_conv_layer(self.h_pnn[tt][1], U_w, U_b, apply_relu=False)
        h = tf.nn.relu(h)
        self.h_pnn[task].append(h)

        # Conv3_x
        h = _residual_block_first(h, filters[2], strides[2], self.trainable_vars[task], self.train_phase[task],
                                  name='conv3_1_t%d' % (task), is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[task], self.train_phase[task], apply_relu=False,
                            name='conv3_2_t%d' % (task))
        # Add lateral connections
        for tt in range(task):
            U_w = weight_variable([1, 1, self.h_pnn[tt][2].get_shape().as_list()[-1], h.get_shape().as_list()[-1]],
                                  name='conv_3_w_t%d_tt%d' % (task, tt))
            U_b = bias_variable([h.get_shape().as_list()[-1]], name='conv_3_b_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            h += create_conv_layer(self.h_pnn[tt][2], U_w, U_b, stride=strides[2], apply_relu=False)
        h = tf.nn.relu(h)
        self.h_pnn[task].append(h)

        # Conv4_x
        h = _residual_block_first(h, filters[3], strides[3], self.trainable_vars[task], self.train_phase[task],
                                  name='conv4_1_t%d' % (task), is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[task], self.train_phase[task], apply_relu=False,
                            name='conv4_2_t%d' % (task))
        # Add lateral connections
        for tt in range(task):
            U_w = weight_variable([1, 1, self.h_pnn[tt][3].get_shape().as_list()[-1], h.get_shape().as_list()[-1]],
                                  name='conv_4_w_t%d_tt%d' % (task, tt))
            U_b = bias_variable([h.get_shape().as_list()[-1]], name='conv_4_b_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            h += create_conv_layer(self.h_pnn[tt][3], U_w, U_b, stride=strides[3], apply_relu=False)
        h = tf.nn.relu(h)
        self.h_pnn[task].append(h)

        # Conv5_x
        h = _residual_block_first(h, filters[4], strides[4], self.trainable_vars[task], self.train_phase[task],
                                  name='conv5_1_t%d' % (task), is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars[task], self.train_phase[task], apply_relu=False,
                            name='conv5_2_t%d' % (task))
        # Add lateral connections
        for tt in range(task):
            U_w = weight_variable([1, 1, self.h_pnn[tt][4].get_shape().as_list()[-1], h.get_shape().as_list()[-1]],
                                  name='conv_5_w_t%d_tt%d' % (task, tt))
            U_b = bias_variable([h.get_shape().as_list()[-1]], name='conv_5_b_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            h += create_conv_layer(self.h_pnn[tt][4], U_w, U_b, stride=strides[4], apply_relu=False)
        h = tf.nn.relu(h)
        self.h_pnn[task].append(h)

        # Apply average pooling
        h = tf.reduce_mean(h, [1, 2])

        if self.network_arch == 'RESNET-S':
            logits = _fc(h, self.total_classes, self.trainable_vars[task], name='fc_1_t%d' % (task), is_cifar=True)
        else:
            logits = _fc(h, self.total_classes, self.trainable_vars[task], name='fc_1_t%d' % (task))
        for tt in range(task):
            h_tt = tf.reduce_mean(self.h_pnn[tt][5], [1, 2])
            U_w = weight_variable([h_tt.get_shape().as_list()[1], self.total_classes],
                                  name='fc_uw_1_t%d_tt%d' % (task, tt))
            U_b = bias_variable([self.total_classes], name='fc_ub_1_t%d_tt%d' % (task, tt))
            self.trainable_vars[task].append(U_w)
            self.trainable_vars[task].append(U_b)
            logits += create_fc_layer(h_tt, U_w, U_b, apply_relu=False)
        self.h_pnn[task].append(logits)

        return logits

    def fc_variables(self, layer_dims):
        """
        Defines variables for a 3-layer fc network
        Args:

        Returns:
        """

        self.weights = []
        self.biases = []
        self.trainable_vars = []

        if self.imp_method == 'AKCL':
            for i in range(len(layer_dims) - 2):
                w = weight_variable([layer_dims[i], layer_dims[i + 1]], name='fc_%d' % (i))
                b = bias_variable([layer_dims[i + 1]], name='fc_%d' % (i))
                self.weights.append(w)
                self.biases.append(b)
                self.trainable_vars.append(w)
                self.trainable_vars.append(b)

            w = weight_variable([layer_dims[len(layer_dims) - 2], layer_dims[len(layer_dims) - 1]],
                                name='fc_%d' % (len(layer_dims) - 2))
            self.weights.append(w)
            self.trainable_vars.append(w)



        else:
            for i in range(len(layer_dims) - 1):
                w = weight_variable([layer_dims[i], layer_dims[i + 1]], name='fc_%d' % (i))
                b = bias_variable([layer_dims[i + 1]], name='fc_%d' % (i))
                self.weights.append(w)
                self.trainable_vars.append(w)
                self.biases.append(b)
                self.trainable_vars.append(b)
                # if i != len(layer_dims) - 2:



    def fc_feedforward(self, h, weights, biases, apply_dropout=False):
        """
        Forward pass through a fc network
        Args:
            h               Input image (tensor)
            weights         List of weights for a fc network
            biases          List of biases for a fc network
            apply_dropout   Whether to apply droupout (True/ False)

        Returns:
            Logits of a fc network
        """

        if self.imp_method == 'AKCL':

            if apply_dropout:
                h = tf.nn.dropout(h, 1)  # Apply dropout on Input?
            for (w, b) in list(zip(weights[:-1], biases)):
                h = create_fc_layer(h, w, b)
                if apply_dropout:
                    h = tf.nn.dropout(h, 1)  # Apply dropout on hidden layers?
            print(weights)
            # Store image features
            self.features = h
            self.image_feature_dim = h.get_shape().as_list()[-1]

            # # RACL can't
            # h = _fc(h, 2, self.trainable_vars, name='vis_1', is_cifar=False)
            # self.net = h
            # h = _fc(self.net, 256, self.trainable_vars, name='vis_2', is_cifar=False)

            # out_dim = 2
            # in_dim = h.get_shape().as_list()[1]
            # stdv = 1.0 / math.sqrt(in_dim)
            # with tf.variable_scope('vis_1'):
            #     # Define the weights and biases for this layer
            #     w1 = tf.get_variable('weights', [in_dim, out_dim], tf.float32,
            #                          initializer=tf.random_uniform_initializer(-stdv, stdv))
            #     self.trainable_vars.append(w1)
            #
            # with tf.variable_scope('vis_2'):
            #     w2 = tf.get_variable('weights2', [out_dim, in_dim], tf.float32,
            #                          initializer=tf.random_uniform_initializer(-stdv, stdv))
            #     # initializer=tf.truncated_normal_initializer(stddev=0.1))
            #     # Append the variable to the trainable variables list
            #     self.trainable_vars.append(w2)
            # h = tf.matmul(h, w1)
            # self.net = h
            # h = tf.matmul(h, w2)

            h = tf.nn.l2_normalize(h, axis=1)  # [10, 160]
            w = tf.nn.l2_normalize(weights[-1], axis=0)  # [160, 100]

            out = tf.matmul(h, w)

        else:

            if apply_dropout:
                h = tf.nn.dropout(h, 1)  # Apply dropout on Input?
            for (w, b) in list(zip(weights, biases))[:-1]:
                # h, _, _ = create_fc_layer(h, w, b)
                h = create_fc_layer(h, w, b)
                if apply_dropout:
                    h = tf.nn.dropout(h, 1)  # Apply dropout on hidden layers?

            # Store image features
            self.features = h
            self.image_feature_dim = h.get_shape().as_list()[-1]

            # A-GEM loss is NaN
            # h = _fc(h, 2, self.trainable_vars, name='vis_1', is_cifar=False)
            # self.net = h
            # h = _fc(self.net, 256, self.trainable_vars, name='vis_2', is_cifar=False)

            # out_dim = 2
            # in_dim = h.get_shape().as_list()[1]
            # stdv = 1.0 / math.sqrt(in_dim)
            # with tf.variable_scope('vis_1'):
            #     # Define the weights and biases for this layer
            #     w1 = tf.get_variable('weights', [in_dim, out_dim], tf.float32,
            #                         initializer=tf.random_uniform_initializer(-stdv, stdv))
            #     w2 = tf.get_variable('weights2', [out_dim, in_dim], tf.float32,
            #                          initializer=tf.random_uniform_initializer(-stdv, stdv))
            #     # initializer=tf.truncated_normal_initializer(stddev=0.1))
            #     # Append the variable to the trainable variables list
            #     self.trainable_vars.append(w1)
            #     self.trainable_vars.append(w2)
            # # h = tf.matmul(h, w1)
            # # self.net = h
            # # h = tf.matmul(h, w2)
            # h2 = tf.matmul(h, w1)
            # self.net = h2
            # h2 = tf.matmul(h2, w2)
            # h = h + h2
            # # # self.net = tf.matmul(h, w1)

            out = create_fc_layer(h, weights[-1], biases[-1], apply_relu=False)
            # out, ws_h, ws_w = create_fc_layer(h, weights[-1], biases[-1], apply_relu=False)
            # self.ws_log = tf.matmul(tf.nn.l2_normalize(ws_h, axis=1), tf.nn.l2_normalize(ws_w, axis=0))

        return out
        # if self.is_aug == True:
        #     return self.out_logits+self.aug_feat
        # else:
        #     return self.out_logits

    def conv_variables(self, kernel, depth):
        """
        Defines variables of a 5xconv-1xFC convolutional network
        Args:

        Returns:
        """
        self.weights = []
        self.biases = []
        self.trainable_vars = []
        div_factor = 1

        for i in range(len(kernel)):
            w = weight_variable([kernel[i], kernel[i], depth[i], depth[i + 1]], name='conv_%d' % (i))
            b = bias_variable([depth[i + 1]], name='conv_%d' % (i))
            self.weights.append(w)
            self.biases.append(b)
            self.trainable_vars.append(w)
            self.trainable_vars.append(b)

            # Since we maxpool after every two conv layers
            if ((i + 1) % 2 == 0):
                div_factor *= 2

        flat_units = (self.image_size // div_factor) * (self.image_size // div_factor) * depth[-1]
        w = weight_variable([flat_units, self.total_classes], name='fc_%d' % (i))
        b = bias_variable([self.total_classes], name='fc_%d' % (i))
        self.weights.append(w)
        self.biases.append(b)
        self.trainable_vars.append(w)
        self.trainable_vars.append(b)

    def conv_feedforward(self, h, weights, biases, apply_dropout=True):
        """
        Forward pass through a convolutional network
        Args:
            h               Input image (tensor)
            weights         List of weights for a conv network
            biases          List of biases for a conv network
            apply_dropout   Whether to apply droupout (True/ False)

        Returns:
            Logits of a conv network
        """
        for i, (w, b) in enumerate(list(zip(weights, biases))[:-1]):

            # Apply conv operation till the second last layer, which is a FC layer
            h = create_conv_layer(h, w, b)

            if ((i + 1) % 2 == 0):

                # Apply max pool after every two conv layers
                h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME')

                # Apply dropout
                if apply_dropout:
                    h = tf.nn.dropout(h, self.keep_prob)

        # Construct FC layers
        shape = h.get_shape().as_list()
        h = tf.reshape(h, [-1, shape[1] * shape[2] * shape[3]])
        # Store image features
        self.features = h
        self.image_feature_dim = h.get_shape().as_list()[-1]

        return create_fc_layer(h, weights[-1], biases[-1], apply_relu=False)

    def vgg_16_conv_feedforward(self, h):
        """
        Forward pass through a VGG 16 network

        Return:
            Logits of a VGG 16 network
        """
        self.trainable_vars = []
        # Conv1
        h = vgg_conv_layer(h, 3, 64, 1, self.trainable_vars, name='conv1_1')
        h = vgg_conv_layer(h, 3, 64, 1, self.trainable_vars, name='conv1_2')
        h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME', name='pool1')
        # Conv2
        h = vgg_conv_layer(h, 3, 128, 1, self.trainable_vars, name='conv2_1')
        h = vgg_conv_layer(h, 3, 128, 1, self.trainable_vars, name='conv2_2')
        h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME', name='pool2')
        # Conv3
        h = vgg_conv_layer(h, 3, 256, 1, self.trainable_vars, name='conv3_1')
        h = vgg_conv_layer(h, 3, 256, 1, self.trainable_vars, name='conv3_2')
        h = vgg_conv_layer(h, 3, 256, 1, self.trainable_vars, name='conv3_3')
        h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME', name='pool3')
        # Conv4
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv4_1')
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv4_2')
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv4_3')
        h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME', name='pool4')
        # Conv5
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv5_1')
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv5_2')
        h = vgg_conv_layer(h, 3, 512, 1, self.trainable_vars, name='conv5_3')
        h = tf.nn.max_pool(h, ksize=[1, 2, 2, 1], strides=[1, 2, 2, 1], padding='SAME', name='pool5')

        # FC layers
        shape = h.get_shape().as_list()
        h = tf.reshape(h, [-1, shape[1] * shape[2] * shape[3]])
        # fc6
        h = vgg_fc_layer(h, 4096, self.trainable_vars, apply_relu=True, name='fc6')
        # fc7
        h = vgg_fc_layer(h, 4096, self.trainable_vars, apply_relu=True, name='fc7')
        # Store image features
        self.features = h
        self.image_feature_dim = h.get_shape().as_list()[-1]
        # fc8
        if self.class_attr is not None:
            # Return the image features
            return h
        else:
            logits = vgg_fc_layer(h, self.total_classes, self.trainable_vars, apply_relu=False, name='fc8')
            return logits

    def resnet18_conv_feedforward(self, h, kernels, filters, strides):
        """
        Forward pass through a ResNet-18 network

        Returns:
            Logits of a resnet-18 conv network
        """
        self.trainable_vars = []

        # Conv1
        h = _conv(h, kernels[0], filters[0], strides[0], self.trainable_vars, name='conv_1')
        h = _bn(h, self.trainable_vars, self.train_phase, name='bn_1')
        h = tf.nn.relu(h)

        # Conv2_x
        h = _residual_block(h, self.trainable_vars, self.train_phase, name='conv2_1')
        h = _residual_block(h, self.trainable_vars, self.train_phase, name='conv2_2')

        # Conv3_x
        h = _residual_block_first(h, filters[2], strides[2], self.trainable_vars, self.train_phase, name='conv3_1',
                                  is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars, self.train_phase, name='conv3_2')

        # Conv4_x
        h = _residual_block_first(h, filters[3], strides[3], self.trainable_vars, self.train_phase, name='conv4_1',
                                  is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars, self.train_phase, name='conv4_2')

        # Conv5_x
        h = _residual_block_first(h, filters[4], strides[4], self.trainable_vars, self.train_phase, name='conv5_1',
                                  is_ATT_DATASET=self.is_ATT_DATASET)
        h = _residual_block(h, self.trainable_vars, self.train_phase, name='conv5_2')

        # Apply average pooling
        h = tf.reduce_mean(h, [1, 2])

        # Store the feature mappings
        self.features = h
        self.image_feature_dim = h.get_shape().as_list()[-1]

        # A-GEM ok   RACL can't
        # h = _fc(h, 2, self.trainable_vars, name='vis_1')
        # self.net = h
        # h = _fc(self.net, 160, self.trainable_vars, name='vis_2')

        # # RACL can't
        # out_dim = 2
        # in_dim = h.get_shape().as_list()[1]
        # stdv = 1.0 / math.sqrt(in_dim)
        # with tf.variable_scope('vis_1'):
        #     # Define the weights and biases for this layer
        #     w1 = tf.get_variable('weights1', [in_dim, out_dim], tf.float32,
        #                         initializer=tf.random_uniform_initializer(-stdv, stdv))
        #     w2 = tf.get_variable('weights2', [out_dim, in_dim], tf.float32,
        #                          initializer=tf.random_uniform_initializer(-stdv, stdv))
        #     # initializer=tf.truncated_normal_initializer(stddev=0.1))
        #     # Append the variable to the trainable variables list
        #     self.trainable_vars.append(w1)
        #     self.trainable_vars.append(w2)
        # h = tf.matmul(h, w1)
        # self.net = h
        # h = tf.matmul(h, w2)

        # weights4_1 = tf.get_variable(name='weights4_1', shape=[160, 2], dtype=tf.float32,
        #                              initializer=tf.contrib.layers.xavier_initializer())
        # self.net = tf.matmul(h, weights4_1)

        if self.class_attr is not None:
            # Return the image features
            return h
        else:
            if self.imp_method == 'AKCL':
                # logits = _fc(h, self.total_classes, self.trainable_vars, name='fc_1')
                out_dim = self.total_classes
                in_dim = h.get_shape().as_list()[1]
                stdv = 1.0 / math.sqrt(in_dim)
                with tf.variable_scope('fc_1'):
                    # Define the weights and biases for this layer
                    w = tf.get_variable('weights', [in_dim, out_dim], tf.float32,
                                        initializer=tf.random_uniform_initializer(-stdv, stdv))
                    # initializer=tf.truncated_normal_initializer(stddev=0.1))
                    # Append the variable to the trainable variables list
                    self.trainable_vars.append(w)

                # Do the FC operation
                # Normalization without bias
                # print(h, w)
                # exit()
                self.features2 = tf.matmul(h, w)
                h = tf.nn.l2_normalize(h, axis=1)  # [10, 160]
                w = tf.nn.l2_normalize(w, axis=0)  # [160, 100]
                logits = tf.matmul(h, w)

                # self.features2 = logits
                self.image_feature_dim2 = h.get_shape().as_list()[-1]

            else:
                if self.network_arch == 'RESNET-S':
                    logits = _fc(h, self.total_classes, self.trainable_vars, name='fc_1', is_cifar=True)
                else:
                    logits = _fc(h, self.total_classes, self.trainable_vars, name='fc_1')

                self.features2 = logits
                self.image_feature_dim2 = h.get_shape().as_list()[-1]

            return logits

    def get_attribute_embedding(self, attr):
        """
        Get attribute embedding using a simple FC network

        Returns:
            Embedding vector of k x ATTR_DIMS
        """
        w = weight_variable([self.attr_dims, self.image_feature_dim], name='attr_embed_w')
        self.trainable_vars.append(w)
        # Return the inner product of attribute matrix and weight vector.
        return tf.matmul(attr, w)  # Dimension should be TOTAL_CLASSES x image_feature_dim

    def loss_and_gradients(self, imp_method):
        """
        Defines task based and surrogate losses and their
        gradients
        Args:

        Returns:
        """
        reg = 0.0
        if imp_method == 'VAN' or imp_method == 'PNN' or imp_method == 'ER' or 'GEM' in imp_method:
            pass
        elif imp_method == 'EWC' or imp_method == 'M-EWC':
            reg = tf.add_n([tf.reduce_sum(tf.square(w - w_star) * f) for w, w_star,
                                                                         f in
                            zip(self.trainable_vars, self.star_vars, self.normalized_fisher_at_minima_vars)])
        elif imp_method == 'PI':
            reg = tf.add_n([tf.reduce_sum(tf.square(w - w_star) * f) for w, w_star,
                                                                         f in
                            zip(self.trainable_vars, self.star_vars, self.big_omega_vars)])
        elif imp_method == 'MAS':
            reg = tf.add_n([tf.reduce_sum(tf.square(w - w_star) * f) for w, w_star,
                                                                         f in
                            zip(self.trainable_vars, self.star_vars, self.hebbian_score_vars)])
        elif imp_method == 'RWALK':
            reg = tf.add_n([tf.reduce_sum(tf.square(w - w_star) * (f + scr)) for w, w_star,
                                                                                 f, scr in
                            zip(self.trainable_vars, self.star_vars, self.normalized_fisher_at_minima_vars,
                                self.normalized_score_vars)])

        """
        # ***** DON't USE THIS WITH MULTI-HEAD SETTING SINCE THIS WILL UPDATE ALL THE WEIGHTS *****
        # If CNN arch, then use the weight decay
        if self.is_ATT_DATASET:
            self.unweighted_entropy += tf.add_n([0.0005 * tf.nn.l2_loss(v) for v in self.trainable_vars if 'weights' in v.name or 'kernel' in v.name])
        """

        if imp_method == 'PNN':
            # Compute the gradients of regularized loss
            self.reg_gradients_vars = []
            for i in range(self.num_tasks):
                self.reg_gradients_vars.append([])
                self.reg_gradients_vars[i] = self.opt.compute_gradients(self.unweighted_entropy[i],
                                                                        var_list=self.trainable_vars[i])
        elif imp_method != 'A-GEM' and imp_method != 'MEGA' and imp_method != 'MEGAD' and imp_method != 'MEGA_RA' and imp_method != 'AKCL':  # For A-GEM we will define the losses and gradients later on
            if imp_method == 'ER' and 'FC-' not in self.network_arch:
                self.reg_loss = tf.add_n(
                    [self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size
            else:
                # Regularized training loss
                self.reg_loss = tf.squeeze(self.unweighted_entropy + self.synap_stgth * reg)
                # Compute the gradients of the vanilla loss
                self.vanilla_gradients_vars = self.opt.compute_gradients(self.unweighted_entropy,
                                                                         var_list=self.trainable_vars)
            # Compute the gradients of regularized loss
            self.reg_gradients_vars = self.opt.compute_gradients(self.reg_loss,
                                                                 var_list=self.trainable_vars)

    def train_op(self):
        """
        Defines the training operation (a single step during training)
        Args:

        Returns:
        """
        if self.imp_method == 'VAN' or self.imp_method == 'ER':
            # Define training operation
            self.train = self.opt.apply_gradients(self.reg_gradients_vars)
        elif self.imp_method == 'PNN':
            # Define training operation
            self.train = [self.opt.apply_gradients(self.reg_gradients_vars[i]) for i in range(self.num_tasks)]
        elif self.imp_method == 'FTR_EXT':
            # Define a training operation for the first and subsequent tasks
            self.train = self.opt.apply_gradients(self.reg_gradients_vars)
            self.train_classifier = self.opt.apply_gradients(self.reg_gradients_vars[-2:])
        else:
            # Get the value of old weights first
            with tf.control_dependencies([self.weights_old_ops_grouped]):
                # Define a training operation
                self.train = self.opt.apply_gradients(self.reg_gradients_vars)

    def init_vars(self):
        """
        Defines different variables that will be used for the
        weight consolidation
        Args:

        Returns:
        """

        if self.imp_method == 'PNN':
            return

        for v in range(len(self.trainable_vars)):

            # List of variables for weight updates
            self.weights_old.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.weights_delta_old_vars.append(
                tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.star_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False,
                                              name=self.trainable_vars[v].name.rsplit(':')[0] + '_star'))

            # List of variables for pathint method
            self.small_omega_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.big_omega_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.big_omega_riemann_vars.append(
                tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))

            # List of variables to store fisher information
            self.fisher_diagonal_at_minima.append(
                tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))

            self.normalized_fisher_at_minima_vars.append(
                tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False, dtype=tf.float32))
            self.tmp_fisher_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.running_fisher_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            self.score_vars.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            # New variables for conv setting for fisher and score normalization
            self.max_fisher_vars.append(tf.Variable(tf.zeros(1), dtype=tf.float32, trainable=False))
            self.min_fisher_vars.append(tf.Variable(tf.zeros(1), dtype=tf.float32, trainable=False))
            self.max_score_vars.append(tf.Variable(tf.zeros(1), dtype=tf.float32, trainable=False))
            self.min_score_vars.append(tf.Variable(tf.zeros(1), dtype=tf.float32, trainable=False))
            self.normalized_score_vars.append(
                tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            if self.imp_method == 'MAS':
                # List of variables to store hebbian information
                self.hebbian_score_vars.append(
                    tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
            elif self.imp_method == 'A-GEM' or self.imp_method == 'S-GEM' or self.imp_method == 'MEGA' or self.imp_method == 'MEGAD' or self.imp_method == 'MEGA_RA' or self.imp_method == 'AKCL':
                self.ref_grads.append(tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))
                self.ref_loss = tf.Variable(0.0, dtype=tf.float32, trainable=False)

                self.old_task_loss = tf.Variable(0.0, dtype=tf.float32, trainable=False)

                self.projected_gradients_list.append(
                    tf.Variable(tf.zeros(self.trainable_vars[v].get_shape()), trainable=False))

    def get_current_weights(self):
        """
        Get the values of current weights
        Note: These weights are different from star_vars as those
        store the weights after training for the last task.
        Args:

        Returns:
        """
        weights_old_ops = []
        weights_delta_old_ops = []
        for v in range(len(self.trainable_vars)):
            weights_old_ops.append(tf.assign(self.weights_old[v], self.trainable_vars[v]))
            weights_delta_old_ops.append(tf.assign(self.weights_delta_old_vars[v], self.trainable_vars[v]))

        self.weights_old_ops_grouped = tf.group(*weights_old_ops)
        self.weights_delta_old_grouped = tf.group(*weights_delta_old_ops)

    def weights_store_ops(self):
        """
        Defines weight restoration operations
        Args:

        Returns:
        """
        restore_weights_ops = []
        set_star_vars_ops = []

        for v in range(len(self.trainable_vars)):
            restore_weights_ops.append(tf.assign(self.trainable_vars[v], self.star_vars[v]))

            set_star_vars_ops.append(tf.assign(self.star_vars[v], self.trainable_vars[v]))

        self.restore_weights = tf.group(*restore_weights_ops)
        self.set_star_vars = tf.group(*set_star_vars_ops)

    def reset_optimizer_ops(self):
        """
        Defines operations to reset the optimizer
        Args:

        Returns:
        """
        # Set the operation for resetting the optimizer
        self.optimizer_slots = [self.opt.get_slot(var, name) for name in self.opt.get_slot_names() \
                                for var in tf.global_variables() if self.opt.get_slot(var, name) is not None]
        self.slot_names = self.opt.get_slot_names()
        self.opt_init_op = tf.variables_initializer(self.optimizer_slots)

    def create_pathint_ops(self):
        """
        Defines operations for path integral-based importance
        Args:

        Returns:
        """
        reset_small_omega_ops = []
        update_small_omega_ops = []
        update_big_omega_ops = []
        update_big_omega_riemann_ops = []

        for v in range(len(self.trainable_vars)):
            # Make sure that the variables are updated before calculating delta(theta)
            with tf.control_dependencies([self.train]):
                update_small_omega_ops.append(tf.assign_add(self.small_omega_vars[v],
                                                            -(self.vanilla_gradients_vars[v][0] * (
                                                                    self.trainable_vars[v] - self.weights_old[v]))))

            # Ops to reset the small omega
            reset_small_omega_ops.append(tf.assign(self.small_omega_vars[v], self.small_omega_vars[v] * 0.0))

            if self.imp_method == 'PI':
                # Update the big omegas at the end of the task using the Eucldeian distance
                update_big_omega_ops.append(tf.assign_add(self.big_omega_vars[v],
                                                          tf.nn.relu(tf.div(self.small_omega_vars[v], (
                                                                  PARAM_XI_STEP + tf.square(
                                                              self.trainable_vars[v] - self.star_vars[v]))))))
            elif self.imp_method == 'RWALK':
                # Update the big omegas after small intervals using distance in riemannian manifold (KL-divergence)
                update_big_omega_riemann_ops.append(tf.assign_add(self.big_omega_riemann_vars[v],
                                                                  tf.nn.relu(tf.div(self.small_omega_vars[v],
                                                                                    (PARAM_XI_STEP +
                                                                                     self.running_fisher_vars[
                                                                                         v] * tf.square(
                                                                                                self.trainable_vars[v] -
                                                                                                self.weights_delta_old_vars[
                                                                                                    v]))))))

        self.update_small_omega = tf.group(*update_small_omega_ops)
        self.reset_small_omega = tf.group(*reset_small_omega_ops)
        if self.imp_method == 'PI':
            self.update_big_omega = tf.group(*update_big_omega_ops)
        elif self.imp_method == 'RWALK':
            self.update_big_omega_riemann = tf.group(*update_big_omega_riemann_ops)
            self.big_omega_riemann_reset = [tf.assign(tensor, tf.zeros_like(tensor)) for tensor in
                                            self.big_omega_riemann_vars]

        if self.imp_method == 'RWALK':
            # For the first task, scale the scores so that division does not have an effect
            self.scale_score = [tf.assign(s, s * 2.0) for s in self.big_omega_riemann_vars]
            # To reduce the rigidity after each task the importance scores are averaged
            self.update_score = [tf.assign_add(scr, tf.div(tf.add(scr, riemm_omega), 2.0))
                                 for scr, riemm_omega in zip(self.score_vars, self.big_omega_riemann_vars)]

            # Get the min and max in each layer of the scores
            self.get_max_score_vars = [tf.assign(var, tf.expand_dims(tf.squeeze(tf.reduce_max(scr, keep_dims=True)),
                                                                     axis=0)) for var, scr in
                                       zip(self.max_score_vars, self.score_vars)]
            self.get_min_score_vars = [tf.assign(var, tf.expand_dims(tf.squeeze(tf.reduce_min(scr, keep_dims=True)),
                                                                     axis=0)) for var, scr in
                                       zip(self.min_score_vars, self.score_vars)]
            self.max_score = tf.reduce_max(tf.convert_to_tensor(self.max_score_vars))
            self.min_score = tf.reduce_min(tf.convert_to_tensor(self.min_score_vars))
            with tf.control_dependencies([self.max_score, self.min_score]):
                self.normalize_scores = [
                    tf.assign(tgt, (var - self.min_score) / (self.max_score - self.min_score + EPSILON))
                    for tgt, var in zip(self.normalized_score_vars, self.score_vars)]

            # Sparsify all the layers except last layer
            sparsify_score_ops = []
            for v in range(len(self.normalized_score_vars) - 2):
                sparsify_score_ops.append(tf.assign(self.normalized_score_vars[v],
                                                    tf.nn.dropout(self.normalized_score_vars[v], self.keep_prob)))

            self.sparsify_scores = tf.group(*sparsify_score_ops)

    def create_fisher_ops(self):
        """
        Defines the operations to compute online update of Fisher
        Args:

        Returns:
        """
        ders = tf.gradients(self.unweighted_entropy, self.trainable_vars)
        fisher_ema_at_step_ops = []
        fisher_accumulate_at_step_ops = []

        # ops for running fisher
        self.set_tmp_fisher = [tf.assign_add(f, tf.square(d)) for f, d in zip(self.tmp_fisher_vars, ders)]

        # Initialize the running fisher to non-zero value
        self.set_initial_running_fisher = [tf.assign(r_f, s_f) for r_f, s_f in zip(self.running_fisher_vars,
                                                                                   self.tmp_fisher_vars)]

        self.set_running_fisher = [tf.assign(f, (1 - self.fisher_ema_decay) * f + (1.0 / self.fisher_update_after) *
                                             self.fisher_ema_decay * tmp) for f, tmp in
                                   zip(self.running_fisher_vars, self.tmp_fisher_vars)]

        self.get_fisher_at_minima = [tf.assign(var, f) for var, f in zip(self.fisher_diagonal_at_minima,
                                                                         self.running_fisher_vars)]

        self.reset_tmp_fisher = [tf.assign(tensor, tf.zeros_like(tensor)) for tensor in self.tmp_fisher_vars]

        # Get the min and max in each layer of the Fisher
        self.get_max_fisher_vars = [
            tf.assign(var, tf.expand_dims(tf.squeeze(tf.reduce_max(scr, keep_dims=True)), axis=0))
            for var, scr in zip(self.max_fisher_vars, self.fisher_diagonal_at_minima)]
        self.get_min_fisher_vars = [
            tf.assign(var, tf.expand_dims(tf.squeeze(tf.reduce_min(scr, keep_dims=True)), axis=0))
            for var, scr in zip(self.min_fisher_vars, self.fisher_diagonal_at_minima)]
        self.max_fisher = tf.reduce_max(tf.convert_to_tensor(self.max_fisher_vars))
        self.min_fisher = tf.reduce_min(tf.convert_to_tensor(self.min_fisher_vars))
        with tf.control_dependencies([self.max_fisher, self.min_fisher]):
            self.normalize_fisher_at_minima = [tf.assign(tgt,
                                                         (var - self.min_fisher) / (
                                                                 self.max_fisher - self.min_fisher + EPSILON))
                                               for tgt, var in zip(self.normalized_fisher_at_minima_vars,
                                                                   self.fisher_diagonal_at_minima)]

        self.clear_attr_embed_reg = tf.assign(self.normalized_fisher_at_minima_vars[-2],
                                              tf.zeros_like(self.normalized_fisher_at_minima_vars[-2]))

        # Sparsify all the layers except last layer
        sparsify_fisher_ops = []
        for v in range(len(self.normalized_fisher_at_minima_vars) - 2):
            sparsify_fisher_ops.append(tf.assign(self.normalized_fisher_at_minima_vars[v],
                                                 tf.nn.dropout(self.normalized_fisher_at_minima_vars[v],
                                                               self.keep_prob)))

        self.sparsify_fisher = tf.group(*sparsify_fisher_ops)

    def combined_fisher_pathint_ops(self):
        """
        Define the operations to refine Fisher information based on parameters convergence
        Args:

        Returns:
        """
        # self.refine_fisher_at_minima = [tf.assign(f, f*(1.0/(s+1e-12))) for f, s in zip(self.fisher_diagonal_at_minima, self.small_omega_vars)]
        self.refine_fisher_at_minima = [tf.assign(f, f * tf.exp(-100.0 * s)) for f, s in
                                        zip(self.fisher_diagonal_at_minima, self.small_omega_vars)]

    def create_hebbian_ops(self):
        """
        Define operations for hebbian measure of importance (MAS)
        """
        # Compute the gradients of mse loss
        self.mse_gradients = tf.gradients(self.mse, self.trainable_vars)
        # with tf.control_dependencies([self.mse_gradients]):
        # Keep on adding gradients to the omega
        self.accumulate_hebbian_scores = [tf.assign_add(omega, tf.abs(grad)) for omega, grad in
                                          zip(self.hebbian_score_vars, self.mse_gradients)]
        # Average across the total images
        self.average_hebbian_scores = [tf.assign(omega, omega * (1.0 / self.train_samples)) for omega in
                                       self.hebbian_score_vars]
        # Reset the hebbian importance variables
        self.reset_hebbian_scores = [tf.assign(omega, tf.zeros_like(omega)) for omega in self.hebbian_score_vars]

    def create_stochastic_gem_ops(self):
        """
        Define operations for Stochastic GEM
        """
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.ref_grads_tsne = ref_grads[-1]
        # Reference gradient for previous tasks
        # print(self.trainable_vars)
        # print(ref_grads)
        # exit()
        self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        print(self.store_ref_grads)

        flat_ref_grads = tf.concat([tf.reshape(grad, [-1]) for grad in self.ref_grads], 0)
        # Grandient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.cur_grads = task_grads[-1]

        flat_task_grads = tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0)

        with tf.control_dependencies([flat_task_grads]):
            dotp = tf.reduce_sum(tf.multiply(flat_task_grads, flat_ref_grads))
            ref_mag = tf.reduce_sum(tf.multiply(flat_ref_grads, flat_ref_grads))
            proj = flat_task_grads - ((dotp / ref_mag) * flat_ref_grads)
            self.reset_violation_count = self.violation_count.assign(0)

            def increment_violation_count():
                with tf.control_dependencies([tf.assign_add(self.violation_count, 1)]):
                    return tf.identity(self.violation_count)

            self.violation_count = tf.cond(tf.greater_equal(dotp, 0), lambda: tf.identity(self.violation_count),
                                           increment_violation_count)
            # 1. Normal AGEM 88.76
            projected_gradients = tf.cond(tf.greater_equal(dotp, 0), lambda: tf.identity(flat_task_grads),
                                          lambda: tf.identity(proj))
            # 2. MEGA way 41.00
            # projected_gradients = tf.cond(tf.less_equal((tf.norm(flat_task_grads) * tf.norm(flat_ref_grads)), 1e-10), lambda: tf.identity(flat_task_grads), lambda: tf.identity(proj))
            # 3. No constrains
            # projected_gradients = proj

            # Convert the flat projected gradient vector into a list
            offset = 0
            store_proj_grad_ops = []
            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(
                    tf.assign(v, tf.reshape(projected_gradients[offset:offset + v_params], shape)))
                offset += v_params
            self.store_proj_grads = tf.group(*store_proj_grad_ops)
            # Define training operations for the tasks > 1
            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    def create_stochastic_mega_ops(self):
        """
        Define operations for MEGA
        """
        #################################################################
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.ref_grads_tsne = ref_grads[-1]

        # Reference gradient for previous tasks
        self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        self.store_ref_loss = tf.assign(self.ref_loss, self.agem_loss)

        #################################################################
        flat_ref_grads = tf.concat([tf.reshape(grad, [-1]) for grad in self.ref_grads], 0)

        # Gradient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.cur_grads = task_grads[-1]
        # print(self.cur_grads)
        # exit()
        flat_task_grads = tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0)

        self.old_task_loss = tf.assign(self.old_task_loss, self.agem_loss)

        with tf.control_dependencies([flat_task_grads, flat_ref_grads]):

            self.deno1 = (tf.norm(flat_task_grads) * tf.norm(flat_ref_grads))
            self.num1 = tf.reduce_sum(tf.multiply(flat_task_grads, flat_ref_grads))
            self.angle_tilda = tf.acos(self.num1 / self.deno1)
            pi = tf.constant(math.pi)

            thetas = []
            objectives = []

            for _ in range(3):
                thetas.append(tf.random_uniform([], 0, math.pi / 2))
                objectives.append(tf.random_uniform([], 0, math.pi / 2))

            self.ratio = tf.div(self.ref_loss, self.old_task_loss)

            def condition(steps, theta):
                return steps <= 10

            def loop(steps, theta):
                theta = theta + (1 / (1 + self.ratio)) * (
                        -tf.sin(theta) + self.ratio * tf.sin(self.angle_tilda - theta))
                theta = tf.cond(tf.greater_equal(theta, 0.5 * pi), lambda: tf.identity(0.5 * pi),
                                lambda: tf.identity(theta))
                theta = tf.cond(tf.less_equal(theta, 0.0), lambda: tf.identity(0.0), lambda: tf.identity(theta))
                steps = tf.add(steps, 1)
                return [steps, theta]

            for idx in range(3):
                steps = tf.constant(0.0)

                _, thetas[idx] = tf.while_loop(
                    condition,
                    loop,
                    [steps, thetas[idx]]
                )

                objectives[idx] = self.old_task_loss * tf.cos(thetas[idx]) + self.ref_loss * tf.cos(
                    self.angle_tilda - thetas[idx])

            objectives = tf.convert_to_tensor(objectives)
            max_idx = tf.argmax(objectives)
            self.theta = tf.gather(thetas, max_idx)

            tr = tf.reduce_sum(tf.multiply(flat_task_grads, flat_ref_grads))
            tt = tf.reduce_sum(tf.multiply(flat_task_grads, flat_task_grads))
            rr = tf.reduce_sum(tf.multiply(flat_ref_grads, flat_ref_grads))

            def compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads):
                a = (rr * tt * tf.cos(self.theta) - tr * tf.norm(flat_task_grads) * tf.norm(flat_ref_grads) * tf.cos(
                    self.angle_tilda - self.theta)) / self.deno
                b = (-tr * tt * tf.cos(self.theta) + tt * tf.norm(flat_task_grads) * tf.norm(flat_ref_grads) * tf.cos(
                    self.angle_tilda - self.theta)) / self.deno
                return a * flat_task_grads + b * flat_ref_grads

            self.deno = tt * rr - tr * tr

            # 1. Normal MEGA
            # g_tilda = tf.cond(tf.less_equal(self.deno, 1e-10), lambda: tf.identity(flat_task_grads), lambda: compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads))
            # 2. tf.norm(flat_ref_grads 
            # g_tilda = tf.cond(tf.equal(tf.norm(flat_ref_grads), 0), lambda: tf.identity(flat_task_grads), lambda: compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads))
            # g_tilda = tf.cond(tf.equal(tf.norm(flat_task_grads) * tf.norm(flat_ref_grads), 0), lambda: tf.identity(flat_task_grads), lambda: compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads))
            # 4. No constraints
            # g_tilda = compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads)
            # 5. AGEM way
            # g_tilda = tf.cond(tf.greater_equal(tf.reduce_sum(tf.multiply(flat_task_grads, flat_ref_grads)), 0), lambda: tf.identity(flat_task_grads), lambda: compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads))
            # 6. different magnitude of ref grads
            g_tilda = tf.cond(tf.less_equal(tf.norm(flat_ref_grads), 1e-4), lambda: tf.identity(flat_task_grads),
                              lambda: compute_g_tilda(tr, tt, rr, flat_task_grads, flat_ref_grads))

            offset = 0
            store_proj_grad_ops = []
            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(tf.assign(v, tf.reshape(g_tilda[offset:offset + v_params], shape)))
                offset += v_params
            self.store_proj_grads = tf.group(*store_proj_grad_ops)

            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        #################################################################################
        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    def create_stochastic_megad_ops(self):
        """
        Define operations for  MEGAD
        """
        #################################################################
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)

        # Reference gradient for previous tasks
        self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        self.store_ref_loss = tf.assign(self.ref_loss, self.agem_loss)

        #################################################################
        flat_ref_grads = tf.concat([tf.reshape(grad, [-1]) for grad in self.ref_grads], 0)
        # Grandient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        flat_task_grads = tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0)

        self.old_task_loss = tf.assign(self.old_task_loss, self.agem_loss)

        with tf.control_dependencies([flat_task_grads, flat_ref_grads]):

            self.ratio = tf.div(self.ref_loss, self.old_task_loss)

            g_tilda = tf.cond(tf.less_equal(self.ref_loss, 1e-10), lambda: tf.identity(flat_ref_grads),
                              lambda: flat_task_grads + self.ratio * flat_ref_grads)
            # g_tilda = flat_task_grads #+ self.ratio * flat_ref_grads

            offset = 0
            store_proj_grad_ops = []
            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(tf.assign(v, tf.reshape(g_tilda[offset:offset + v_params], shape)))
                offset += v_params
            self.store_proj_grads = tf.group(*store_proj_grad_ops)

            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        #################################################################################

        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    def create_stochastic_akcl_ops(self):
        """
        Define operations for AKCL
        """
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM' or self.imp_method == 'EMP':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        # Reference gradient for previous tasks
        # self.tt = ref_grads
        self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        self.store_ref_loss = tf.assign(self.ref_loss, self.agem_loss)

        # self.store_ref_grads_cache = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        # self.store_del_ref_loss_cache = tf.assign(self.del_ref_loss_cache, self.agem_loss)
        flat_ref_grads = tf.reshape(tf.concat([tf.reshape(grad, [-1]) for grad in ref_grads], 0), [1, -1])

        self.ref_grads_save = flat_ref_grads

        KL_grads = tf.gradients(self.KL_loss, self.trainable_vars)
        KL_grads = KL_grads[:-1]
        if self.network_arch == 'RESNET-S':
            grad_offset1 = tf.zeros([160, self.total_classes])
        elif self.network_arch == 'RESNET-B':
            grad_offset1 = tf.zeros([512, self.total_classes])
        else:
            grad_offset1 = tf.zeros([256, self.total_classes])

        KL_grads.append(grad_offset1)
        # print(KL_grads)
        # exit()
        # grad_offset2 = tf.zeros([self.total_classes])
        # KL_grads.append(grad_offset2)
        flat_kl_grads = tf.reshape(tf.concat([tf.reshape(grad, [-1]) for grad in KL_grads], 0), [1, -1])
        self.kl_grads_save = flat_kl_grads
        # Grandient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        flat_task_grads = tf.reshape(tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0), [1, -1])
        current_loss = self.agem_loss
        with tf.control_dependencies([flat_task_grads, current_loss]):
            total_loss = tf.concat([self.store_loss, tf.expand_dims(current_loss, 0)], axis=0)
            self.total_loss = total_loss
            loss_ratio = tf.div(total_loss, current_loss)
            self.ratio = loss_ratio
            # grads = tf.concat([self.store_grads, flat_task_grads], axis=0)
            grads = tf.concat([self.store_grads, self.store_kl_grads, flat_task_grads], axis=0)
            grads_sum = tf.reduce_sum(grads, axis=-1)
            grads_zero_mask = tf.cast(tf.not_equal(grads_sum, 0), tf.float32)  # to omit zero grads
            # # grad_weights = tf.expand_dims(tf.nn.softmax(tf.concat([self.store_del_ref_loss, self.del_cur_loss], axis=0)), axis=1) # tx1
            angles = tf.reduce_sum(grads * flat_task_grads, axis=-1) / (
                    tf.norm(grads, axis=-1) * tf.norm(flat_task_grads, axis=-1))
            angles_filter = tf.cast(tf.greater(angles, 0), tf.float32)  # to omit zero grads
            # angles = 
            angles = tf.expand_dims(1. - masked_softmax(angles, angles_filter * grads_zero_mask), axis=1)
            loss_ratio = tf.expand_dims(loss_ratio, axis=1)
            # loss_ratio = tf.expand_dims(masked_softmax(loss_ratio, angles_filter*grads_zero_mask), axis=1)
            self.angle = angles
            grads = grads * angles  # weighted grads
            # proj = tf.reduce_sum(grads, axis=0)#/(tf.reduce_sum(angles_filter*grads_zero_mask)+1)#/(current_task_id+1.)
            proj = tf.reduce_mean(flat_task_grads, axis=0) + tf.reduce_sum(grads[:-1],
                                                                           axis=0)  # /(tf.reduce_sum(angles_filter*grads_zero_mask)+1)#/(current_task_id+1.)
            # proj = tf.reduce_sum(grads, axis=0)#/(tf.reduce_sum(angles_filter*grads_zero_mask))#/(current_task_id+1.)
            # dotp = tf.reduce_sum(tf.multiply(flat_task_grads, tf.expand_dims(proj, axis=0)))

            # self.reset_violation_count = self.violation_count.assign(0)
            # def increment_violation_count():
            #     with tf.control_dependencies([tf.assign_add(self.violation_count, 1)]):
            #         return tf.identity(self.violation_count)
            # # self.violation_count = tf.cond(tf.greater_equal(dotp, 0), lambda: tf.identity(self.violation_count), increment_violation_count)
            # # projected_gradients = tf.cond(tf.greater_equal(dotp, 0), lambda: tf.identity(tf.squeeze(flat_task_grads, 0)), lambda: tf.identity(proj))
            # self.violation_count = increment_violation_count()

            # projected_gradients = proj
            projected_gradients = tf.reduce_mean(self.store_grads + self.store_kl_grads + flat_task_grads, axis=0)
            # projected_gradients = tf.reduce_mean(self.store_grads + flat_task_grads, axis=0)

            # Convert the flat projected gradient vector into a list
            offset = 0
            store_proj_grad_ops = []

            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(
                    tf.assign(v, tf.reshape(projected_gradients[offset:offset + v_params], shape)))
                offset += v_params
            self.store_proj_grads = tf.group(*store_proj_grad_ops)
            # Define training operations for the tasks > 1
            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    def create_stochastic_megad_ops2(self):
        """
        Define operations for  MEGAD
        """
        #################################################################
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.ref_grads_tsne = ref_grads[-1]

        # Reference gradient for previous tasks
        self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        self.store_ref_loss = tf.assign(self.ref_loss, self.agem_loss)

        #################################################################
        flat_ref_grads = tf.concat([tf.reshape(grad, [-1]) for grad in self.ref_grads], 0)
        # Grandient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        self.cur_grads = task_grads[-1]
        flat_task_grads = tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0)

        self.old_task_loss = tf.assign(self.old_task_loss, self.agem_loss)

        with tf.control_dependencies([flat_task_grads, flat_ref_grads]):

            # 1. normal MEGAD
            self.ratio = tf.div(self.ref_loss, self.old_task_loss)
            # 2. revere MEGAD
            # self.ratio  = tf.div(self.old_task_loss, self.ref_loss)
            # self.ratio  = tf.div(tf.norm(flat_task_grads), tf.norm(flat_ref_grads))
            # 3. some attemption
            # self.ratio  = tf.div(tf.norm(flat_ref_grads), tf.norm(flat_task_grads))
            # self.ratio  = (1 + tf.reduce_sum(tf.multiply(flat_task_grads, flat_ref_grads))/(tf.norm(flat_task_grads)*tf.norm(flat_ref_grads))) / 4 + 3/4
            # 4. loss control
            # self.ratio = tf.div(self.ref_loss, self.last_ref_loss)

            # g_tilda = tf.cond(tf.less_equal(self.ref_loss, 1e-9), lambda: tf.identity(flat_task_grads), lambda: self.ratio * flat_task_grads + flat_ref_grads)
            ab = tf.nn.softmax([(self.old_task_loss + 1e-7) / (self.last_cur_loss + 1e-7),
                                (self.ref_loss + 1e-7) / (self.last_ref_loss + 1e-7)])
            self.ab = ab
            g_tilda = 0.5 * flat_task_grads + 0.5 * flat_ref_grads

            # layer-wise gradient combination
            # g_tilda = []
            # for gr, gt in zip(self.ref_grads, task_grads):
            #     layer_ratio = 1
            #     gr_flat = tf.reshape(gr, [-1])
            #     gt_flat = tf.reshape(gt, [-1])
            #     # ratio  = tf.div(tf.norm(gr_flat), tf.norm(gt_flat))
            #     # gc = tf.cond(tf.less_equal(tf.norm(gr_flat), 1e-5), lambda: tf.identity(gt), lambda: gr + ratio * gt)
            #     # a = tf.reduce_sum(tf.matmul(tf.reshape(tf.transpose(gr_flat-gt_flat), [1, -1]), tf.reshape(gt_flat, [-1, 1]))/tf.norm(gr_flat-gt_flat))
            #     # a = tf.maximum(tf.minimum(a,1),0)
            #     # b = 1 - a
            #     # softmax
            #     # ab = tf.nn.softmax([self.old_task_loss, self.ref_loss])
            #     # norm
            #     # a = 1
            #     # b = 1
            #     # delta loss
            #     # a = 1
            #     # b = 1
            #     ab = tf.nn.softmax([(self.old_task_loss+1e-7)/(self.last_cur_loss+1e-7), (self.ref_loss+1e-7)/(self.last_ref_loss+1e-7)])
            #     self.ab = ab
            #     gc = ab[0] * gt + ab[1] * gr
            #     # gc = tf.cond(tf.greater_equal(tf.reduce_sum(tf.multiply(gr_flat, gt_flat)), 0), 
            #     #             lambda: tf.identity(a * gt + b * gr), 
            #     #             lambda: tf.identity(0 * gt + b * gr))
            #     g_tilda.append(gc)

            store_proj_grad_ops = []
            # Normal
            offset = 0
            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(tf.assign(v, tf.reshape(g_tilda[offset:offset + v_params], shape)))
                offset += v_params
            # layer-wise
            # for v, g in zip(self.projected_gradients_list, g_tilda):
            #     store_proj_grad_ops.append(tf.assign(v, g))
            self.store_proj_grads = tf.group(*store_proj_grad_ops)

            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        #################################################################################

        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    def create_stochastic_megara_ops(self):
        """
        Define operations for EMP
        """
        if 'FC-' in self.network_arch or self.imp_method == 'S-GEM' or self.imp_method == 'EMP':
            self.agem_loss = self.unweighted_entropy
        else:
            self.agem_loss = tf.add_n([self.unweighted_entropy[i] for i in range(self.num_tasks)]) / self.mem_batch_size

        ref_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        # Reference gradient for previous tasks
        # self.tt = ref_grads
        # self.store_ref_grads = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        # self.store_ref_loss = tf.assign(self.ref_loss, self.agem_loss)

        # self.store_ref_grads_cache = [tf.assign(ref, grad) for ref, grad in zip(self.ref_grads, ref_grads)]
        # self.store_del_ref_loss_cache = tf.assign(self.del_ref_loss_cache, self.agem_loss)
        flat_ref_grads = tf.reshape(tf.concat([tf.reshape(grad, [-1]) for grad in ref_grads], 0), [1, -1])

        self.ref_grads_save = flat_ref_grads
        # Grandient on the current task
        task_grads = tf.gradients(self.agem_loss, self.trainable_vars)
        flat_task_grads = tf.reshape(tf.concat([tf.reshape(grad, [-1]) for grad in task_grads], 0), [1, -1])
        current_loss = self.agem_loss
        with tf.control_dependencies([flat_task_grads]):
            total_loss = tf.concat([self.store_loss, tf.expand_dims(current_loss, 0)], axis=0)
            self.total_loss = total_loss
            loss_ratio = tf.div(total_loss, current_loss)
            self.ratio = loss_ratio
            grads = tf.concat([self.store_grads, flat_task_grads], axis=0)
            grads_sum = tf.reduce_sum(grads, axis=-1)
            grads_zero_mask = tf.cast(tf.not_equal(grads_sum, 0), tf.float32)  # to omit zero grads
            # # grad_weights = tf.expand_dims(tf.nn.softmax(tf.concat([self.store_del_ref_loss, self.del_cur_loss], axis=0)), axis=1) # tx1
            angles = tf.reduce_sum(grads * flat_task_grads, axis=-1) / (
                    tf.norm(grads, axis=-1) * tf.norm(flat_task_grads, axis=-1))
            angles_filter = tf.cast(tf.greater(angles, 0), tf.float32)  # to omit zero grads
            # angles = 
            angles = tf.expand_dims(1. - masked_softmax(angles, angles_filter * grads_zero_mask), axis=1)
            loss_ratio = tf.expand_dims(loss_ratio, axis=1)
            # loss_ratio = tf.expand_dims(masked_softmax(loss_ratio, angles_filter*grads_zero_mask), axis=1)
            self.angle = angles
            grads = grads * angles  # weighted grads
            # grads = grads*loss_ratio # weighted grads
            # proj = tf.reduce_sum(grads, axis=0)#/(tf.reduce_sum(angles_filter*grads_zero_mask)+1)#/(current_task_id+1.)
            # proj = tf.reduce_mean(flat_task_grads, axis=0)+grads[0]#tf.reduce_mean(grads[0], axis=0)#/(tf.reduce_sum(angles_filter*grads_zero_mask)+1)#/(current_task_id+1.)
            proj = tf.reduce_mean(flat_task_grads, axis=0) + tf.reduce_sum(grads[:-1], axis=0) / (
                    tf.reduce_sum(angles_filter * grads_zero_mask) + 1)  # /(current_task_id+1.)
            # dotp = tf.reduce_sum(tf.multiply(flat_task_grads, tf.expand_dims(proj, axis=0)))

            projected_gradients = proj

            # Convert the flat projected gradient vector into a list
            offset = 0
            store_proj_grad_ops = []

            for v in self.projected_gradients_list:
                shape = v.get_shape()
                v_params = 1
                for dim in shape:
                    v_params *= dim.value
                store_proj_grad_ops.append(
                    tf.assign(v, tf.reshape(projected_gradients[offset:offset + v_params], shape)))
                offset += v_params
            self.store_proj_grads = tf.group(*store_proj_grad_ops)
            # Define training operations for the tasks > 1
            with tf.control_dependencies([self.store_proj_grads]):
                self.train_subseq_tasks = self.opt.apply_gradients(
                    zip(self.projected_gradients_list, self.trainable_vars))

        # Define training operations for the first task
        self.first_task_gradients_vars = self.opt.compute_gradients(self.agem_loss, var_list=self.trainable_vars)
        self.train_first_task = self.opt.apply_gradients(self.first_task_gradients_vars)

    #################################################################################
    #### External APIs of the class. These will be called/ exposed externally #######
    #################################################################################
    def reset_optimizer(self, sess):
        """
        Resets the optimizer state
        Args:
            sess        TF session

        Returns:
        """

        # Call the reset optimizer op
        sess.run(self.opt_init_op)

    def set_active_outputs(self, sess, labels):
        """
        Set the mask for the labels seen so far
        Args:
            sess        TF session
            labels      Mask labels

        Returns:
        """
        new_mask = np.zeros(self.total_classes)
        new_mask[labels] = 1.0
        """
        for l in labels:
            new_mask[l] = 1.0
        """
        sess.run(self.output_mask.assign(new_mask))

    def init_updates(self, sess):
        """
        Initialization updates
        Args:
            sess        TF session

        Returns:
        """
        # Set the star values to the initial weights, so that we can calculate
        # big_omegas reliably
        if self.imp_method != 'PNN':
            sess.run(self.set_star_vars)

    def task_updates(self, sess, task, train_x, train_labels, num_classes_per_task=10, class_attr=None,
                     online_cross_val=False):
        """
        Updates different variables when a task is completed
        Args:
            sess                TF session
            task                Task ID
            train_x             Training images for the task
            train_labels        Labels in the task
            class_attr          Class attributes (only needed for ZST transfer)
        Returns:
        """
        if self.imp_method == 'VAN' or self.imp_method == 'PNN':
            # We'll store the current parameters at the end of this function
            pass
        elif self.imp_method == 'EWC':
            # Get the fisher at the end of a task
            sess.run(self.get_fisher_at_minima)
            # Normalize the fisher
            sess.run([self.get_max_fisher_vars, self.get_min_fisher_vars])
            sess.run([self.min_fisher, self.max_fisher, self.normalize_fisher_at_minima])
            # Don't regularize over the attribute-embedding vectors
            # sess.run(self.clear_attr_embed_reg)
            # Reset the tmp fisher vars
            sess.run(self.reset_tmp_fisher)
        elif self.imp_method == 'M-EWC':
            # Get the fisher at the end of a task
            sess.run(self.get_fisher_at_minima)
            # Refine Fisher based on the convergence info
            sess.run(self.refine_fisher_at_minima)
            # Normalize the fisher
            sess.run([self.get_max_fisher_vars, self.get_min_fisher_vars])
            sess.run([self.min_fisher, self.max_fisher, self.normalize_fisher_at_minima])
            # Reset the tmp fisher vars
            sess.run(self.reset_tmp_fisher)
            # Reset the small_omega_vars
            sess.run(self.reset_small_omega)
        elif self.imp_method == 'PI':
            # Update big omega variables
            sess.run(self.update_big_omega)
            # Reset the small_omega_vars because big_omega_vars are updated before it
            sess.run(self.reset_small_omega)
        elif self.imp_method == 'RWALK':
            if task == 0:
                # If first task then scale by a factor of 2, so that subsequent averaging does not hurt
                sess.run(self.scale_score)
            # Get the updated importance score
            sess.run(self.update_score)
            # Normalize the scores
            sess.run([self.get_max_score_vars, self.get_min_score_vars])
            sess.run([self.min_score, self.max_score, self.normalize_scores])
            # Sparsify scores
            """
            # TODO: Tmp remove this?
            kp = 0.8 + (task*0.5)
            if (kp > 1):
                kp = 1.0
            """
            # sess.run(self.sparsify_scores, feed_dict={self.keep_prob: kp})
            # Get the fisher at the end of a task
            sess.run(self.get_fisher_at_minima)
            # Normalize fisher
            sess.run([self.get_max_fisher_vars, self.get_min_fisher_vars])
            sess.run([self.min_fisher, self.max_fisher, self.normalize_fisher_at_minima])
            # Sparsify fisher
            # sess.run(self.sparsify_fisher, feed_dict={self.keep_prob: kp})
            # Store the weights
            sess.run(self.weights_delta_old_grouped)
            # Reset the small_omega_vars because big_omega_vars are updated before it
            sess.run(self.reset_small_omega)
            # Reset the big_omega_riemann because importance score is stored in the scores array
            sess.run(self.big_omega_riemann_reset)
            # Reset the tmp fisher vars
            sess.run(self.reset_tmp_fisher)
        elif self.imp_method == 'MAS':
            # zero out any previous values
            sess.run(self.reset_hebbian_scores)
            if self.class_attr is not None:
                # Define mask based on the class attributes
                masked_class_attrs = np.zeros_like(class_attr)
                masked_class_attrs[train_labels] = class_attr[train_labels]
            # Logits mask
            logit_mask = np.zeros(self.total_classes)
            logit_mask[train_labels] = 1.0

            # Loop over the entire training dataset to compute the parameter importance
            batch_size = 10
            num_samples = train_x.shape[0]
            for iters in range(num_samples // batch_size):
                offset = iters * batch_size
                if self.class_attr is not None:
                    sess.run(self.accumulate_hebbian_scores,
                             feed_dict={self.x: train_x[offset:offset + batch_size], self.keep_prob: 1.0,
                                        self.class_attr: masked_class_attrs, self.output_mask: logit_mask,
                                        self.train_phase: False})
                else:
                    sess.run(self.accumulate_hebbian_scores,
                             feed_dict={self.x: train_x[offset:offset + batch_size], self.keep_prob: 1.0,
                                        self.output_mask: logit_mask, self.train_phase: False})

            # Average the hebbian scores across the training examples
            sess.run(self.average_hebbian_scores, feed_dict={self.train_samples: num_samples})

        # Store current weights
        self.init_updates(sess)

    def restore(self, sess):
        """
        Restore the weights from the star variables
        Args:
            sess        TF session

        Returns:
        """
        sess.run(self.restore_weights)
