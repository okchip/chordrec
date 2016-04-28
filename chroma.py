from __future__ import print_function
import os
import collections
import theano.tensor as tt
import lasagne as lnn
import yaml
import numpy as np

import nn
import dmgr
from nn.utils import Colors

import test
import data
import features
import targets
import augmenters
import experiment
import dnn
import convnet


def compute_chroma_loss(prediction, target):
    # need to clip predictions for numerical stability
    eps = 1e-7
    pred_clip = tt.clip(prediction, eps, 1.-eps)
    return lnn.objectives.binary_crossentropy(pred_clip, target).mean()


def build_net(feature_shape, out_size_chroma, out_size_chords,
              chroma_extractor):

    # input variables
    input_var = (tt.tensor3('feature_input', dtype='float32')
                 if len(feature_shape) > 1 else
                 tt.matrix('feature_input', dtype='float32'))
    crm_target_var = tt.matrix('chroma_output', dtype='float32')
    crd_target_var = tt.matrix('chord_output', dtype='float32')

    # stack more layers
    network = lnn.layers.InputLayer(
        name='input', shape=(None,) + feature_shape, input_var=input_var)

    net = chroma_extractor['net']
    if chroma_extractor['type'] == 'conv':
        # reshape to 1 "color" channel
        network = lnn.layers.reshape(
            network, shape=(-1, 1) + feature_shape, name='reshape')

        network = convnet.stack_layers(network, **net['conv'])
        if net['dense']:
            network = dnn.stack_layers(network, **net['dense'])
            crm = lnn.layers.DenseLayer(
                network, name='chroma_out', num_units=out_size_chroma,
                nonlinearity=lnn.nonlinearities.sigmoid)
        elif net['global_avg_pool']:
            crm = convnet.stack_gap(
                network, out_size_chroma,
                output_nonlinearity=lnn.nonlinearities.sigmoid,
                **net['global_avg_pool']
            )
            crm.name = 'chroma_out'
        else:
            raise ValueError('Need to specify output architecture!')

    elif chroma_extractor['type'] == 'dense':
        dense = chroma_extractor['net']
        network = dnn.stack_layers(
            net=network,
            batch_norm=dense['batch_norm'],
            nonlinearity=dense['nonlinearity'],
            num_layers=dense['num_layers'],
            num_units=dense['num_units'],
            dropout=dense['dropout']
        )
        crm = lnn.layers.DenseLayer(
            network, name='chroma_out', num_units=out_size_chroma,
            nonlinearity=lnn.nonlinearities.sigmoid)

    crds = lnn.layers.DenseLayer(
        crm, name='chords', num_units=out_size_chords,
        nonlinearity=lnn.nonlinearities.softmax)

    # tag chord classification parameters so we can distinguish them later
    for p in crds.get_params():
        crds.params[p].add('chord')

    return crm, crds, input_var, crm_target_var, crd_target_var


def compute_chroma(process_fn, agg_dataset, dest_dir, batch_size,
                   extension='.chroma.npy'):
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    chroma_files = []

    for ds_idx in range(agg_dataset.n_datasources):
        ds = agg_dataset.datasource(ds_idx)

        chromas = []

        for data, _ in dmgr.iterators.iterate_batches(ds, batch_size,
                                                      randomise=False,
                                                      expand=False):
            chromas.append(process_fn(data))

        chromas = np.concatenate(chromas)
        chroma_file = os.path.join(dest_dir, ds.name + extension)
        np.save(chroma_file, chromas)
        chroma_files.append(chroma_file)

    return chroma_files


# Initialise Sacred experiment
ex = experiment.setup('Chroma Extractor')


@ex.config
def config():
    observations = 'results'

    datasource = dict(
        context_size=7,
    )

    feature_extractor = None

    target = None

    chroma_extractor = None

    optimiser = dict(
        name='adam',
        params=dict(
            learning_rate=0.001
        ),
        schedule=None
    )

    training = dict(
        num_epochs=500,
        early_stop=20,
        batch_size=512,
        early_stop_acc=True,
    )

    testing = dict(
        test_on_val=False,
        batch_size=training['batch_size']
    )

    augmentation = None


@ex.named_config
def dense_net():
    chroma_extractor = dict(
        type='dense',
        net=dict(
            num_layers=3,
            num_units=512,
            dropout=0.5,
            nonlinearity='rectify',
            batch_norm=False,
        ),
        optimiser=dict(
            name='adam',
            params=dict(
                learning_rate=0.0001
            ),
            schedule=None
        ),
        training=dict(
            iterator='BatchIterator',
            batch_size=512,
            num_epochs=500,
            early_stop=20,
            early_stop_acc=False,
        ),
        regularisation=dict(
            l1=0.0,
            l2=1e-4
        )
    )


@ex.named_config
def conv_net():
    chroma_extractor = dict(
        type='conv',
        net=dict(
            conv=dict(
                batch_norm=True,
                conv1=dict(
                    num_layers=4,
                    num_filters=32,
                    filter_size=(3, 3),
                    pool_size=(1, 2),
                    dropout=0.5,
                    pad='same'
                ),
                conv2=dict(
                    num_layers=2,
                    num_filters=64,
                    filter_size=(3, 3),
                    pool_size=(1, 2),
                    dropout=0.5,
                    pad='valid'
                ),
                conv3={},
            ),
            dense=None,
            global_avg_pool=dict(
                num_filters=128,
                filter_size=(9, 12),
                dropout=0.5,
                batch_norm=True
            )
        ),
        optimiser=dict(
            name='adam',
            params=dict(
                learning_rate=0.001
            ),
            schedule=None
        ),
        training=dict(
            iterator='BatchIterator',
            batch_size=512,
            num_epochs=500,
            early_stop=5,
            early_stop_acc=False,
        ),
        regularisation=dict(
            l2=1e-7,
            l1=0
        )
    )


@ex.named_config
def dense_classifier():
    chroma_extractor = dict(
        net=dict(
            global_avg_pool=None,
            dense=dict(
                num_layers=1,
                num_units=512,
                dropout=0.5,
                nonlinearity='rectify',
                batch_norm=False
            )
        )
    )


@ex.named_config
def no_context():
    datasource = dict(
        context_size=0
    )


@ex.automain
def main(datasource, feature_extractor, chroma_extractor, target, optimiser,
         training, testing, augmentation):

    if feature_extractor is None:
        print(Colors.red('ERROR: Specify a feature extractor!'))
        return 1

    if chroma_extractor is None:
        print(Colors.red('ERROR: Specify a chroma extractor!'))
        return 1

    # TODO: is there a nicer solution?
    if augmentation is not None:
        augmentation['SemitoneShift']['target_type'] = 'chroma'

    if not isinstance(datasource['test_fold'], collections.Iterable):
        datasource['test_fold'] = [datasource['test_fold']]

    if not isinstance(datasource['val_fold'], collections.Iterable):
        datasource['val_fold'] = [datasource['val_fold']]

        # if no validation folds are specified, always use the
        # 'None' and determine validation fold automatically
        if datasource['val_fold'][0] is None:
            datasource['val_fold'] *= len(datasource['test_fold'])

    if len(datasource['test_fold']) != len(datasource['val_fold']):
        print(Colors.red('ERROR: Need same number of validation and '
                         'test folds'))
        return 1

    all_pred_files = []
    all_gt_files = []

    print(Colors.magenta('\nStarting experiment ' + ex.observers[0].hash()))

    with experiment.TempDir() as exp_dir:
        for test_fold, val_fold in zip(datasource['test_fold'],
                                       datasource['val_fold']):
            print('')
            print(Colors.yellow(
                '=' * 20 + ' FOLD {} '.format(test_fold) + '=' * 20))
            # Load data sets
            print(Colors.red('\nLoading data...\n'))

            target_chroma = targets.ChromaTarget(
                feature_extractor['params']['fps'])

            target_chords = targets.create_target(
                feature_extractor['params']['fps'],
                target
            )

            feature_ext = features.create_extractor(feature_extractor)

            train_set, val_set, test_set, gt_files = data.create_datasources(
                dataset_names=datasource['datasets'],
                preprocessors=datasource['preprocessors'],
                compute_features=feature_ext,
                compute_targets=target_chroma,
                context_size=datasource['context_size'],
                test_fold=test_fold,
                val_fold=val_fold,
                cached=datasource['cached']
            )

            if testing['test_on_val']:
                test_set = val_set

            print(Colors.blue('Train Set:'))
            print('\t', train_set)

            print(Colors.blue('Validation Set:'))
            print('\t', val_set)

            print(Colors.blue('Test Set:'))
            print('\t', test_set)
            print('')

            # build network
            print(Colors.red('Building network...\n'))

            chroma_net, chord_net, input_var, chroma_var, chord_var = (
                build_net(
                    feature_shape=train_set.dshape,
                    out_size_chroma=train_set.tshape[0],
                    out_size_chords=target_chords.num_classes,
                    chroma_extractor=chroma_extractor,
                )
            )

            chroma_optimiser, chroma_lrs = experiment.create_optimiser(
                chroma_extractor['optimiser'])

            chroma_train_fn = nn.compile_train_fn(
                chroma_net, input_var, chroma_var,
                loss_fn=compute_chroma_loss, opt_fn=chroma_optimiser,
                **chroma_extractor['regularisation']
            )

            chroma_test_fn = nn.compile_test_func(
                chroma_net, input_var, chroma_var,
                loss_fn=compute_chroma_loss,
                **chroma_extractor['regularisation']
            )

            chroma_process_fn = nn.compile_process_func(
                chroma_net, input_var
            )

            chord_optimiser, chord_lrs = experiment.create_optimiser(optimiser)

            chord_train_fn = nn.compile_train_fn(
                chord_net, input_var, chord_var,
                loss_fn=dnn.compute_loss, opt_fn=chord_optimiser,
                tags={'chord': True}, **chroma_extractor['regularisation']
            )

            chord_test_fn = nn.compile_test_func(
                chord_net, input_var, chord_var,
                loss_fn=dnn.compute_loss,
                tags={'chord': True}, **chroma_extractor['regularisation']
            )

            chord_process_fn = nn.compile_process_func(
                chord_net, input_var
            )

            print(Colors.blue('Chroma Network:'))
            print(nn.to_string(chroma_net))
            print('')

            print(Colors.blue('Chords Network:'))
            print(nn.to_string(chord_net))
            print('')

            print(Colors.red('Starting training chroma network...\n'))

            chroma_training = chroma_extractor['training']
            train_batches = experiment.train_iterator(
                train_set, chroma_training)
            validation_batches = dmgr.iterators.BatchIterator(
                val_set, chroma_training['batch_size'], randomise=False,
                expand=False
            )

            if augmentation is not None:
                train_batches = dmgr.iterators.AugmentedIterator(
                    train_batches, *augmenters.create_augmenters(augmentation)
                )

            crm_train_losses, crm_val_losses, _, crm_val_accs = nn.train(
                network=chroma_net,
                train_fn=chroma_train_fn, train_batches=train_batches,
                test_fn=chroma_test_fn, validation_batches=validation_batches,
                threads=None, callbacks=[chroma_lrs] if chroma_lrs else [],
                num_epochs=chroma_training['num_epochs'],
                early_stop=chroma_training['early_stop'],
                early_stop_acc=chroma_training['early_stop_acc'],
                acc_func=nn.nn.elemwise_acc
            )

            # we need to create a new dataset with a new target (chords)
            del train_set
            del val_set
            del test_set
            del gt_files

            train_set, val_set, test_set, gt_files = data.create_datasources(
                dataset_names=datasource['datasets'],
                preprocessors=datasource['preprocessors'],
                compute_features=feature_ext,
                compute_targets=target_chords,
                context_size=datasource['context_size'],
                test_fold=test_fold,
                val_fold=val_fold,
                cached=datasource['cached']
            )

            if testing['test_on_val']:
                test_set = val_set

            print(Colors.blue('Train Set:'))
            print('\t', train_set)

            print(Colors.blue('Validation Set:'))
            print('\t', val_set)

            print(Colors.blue('Test Set:'))
            print('\t', test_set)
            print('')

            print(Colors.red('Starting training chord network...\n'))

            train_batches = experiment.train_iterator(
                train_set, training)
            validation_batches = dmgr.iterators.BatchIterator(
                val_set, training['batch_size'], randomise=False,
                expand=False
            )

            if augmentation is not None:
                train_batches = dmgr.iterators.AugmentedIterator(
                    train_batches, *augmenters.create_augmenters(augmentation)
                )

            crd_train_losses, crd_val_losses, _, crd_val_accs = nn.train(
                network=chord_net,
                train_fn=chord_train_fn, train_batches=train_batches,
                test_fn=chord_test_fn, validation_batches=validation_batches,
                threads=10, callbacks=[chord_lrs] if chord_lrs else [],
                num_epochs=training['num_epochs'],
                early_stop=training['early_stop'],
                early_stop_acc=training['early_stop_acc'],
            )

            print(Colors.red('\nStarting testing...\n'))

            param_file = os.path.join(
                exp_dir, 'params_fold_{}.pkl'.format(test_fold))
            nn.save_params(chord_net, param_file)
            ex.add_artifact(param_file)

            pred_files = test.compute_labeling(
                chord_process_fn, target_chords, test_set, dest_dir=exp_dir,
                use_mask=False, batch_size=testing['batch_size']
            )

            # compute chroma vectors for the test set
            for cf in compute_chroma(chroma_process_fn, test_set,
                                     batch_size=training['batch_size'],
                                     dest_dir=exp_dir):
                ex.add_artifact(cf)

            test_gt_files = dmgr.files.match_files(
                pred_files, test.PREDICTION_EXT, gt_files, data.GT_EXT
            )

            all_pred_files += pred_files
            all_gt_files += test_gt_files

            print(Colors.blue('Results:'))
            scores = test.compute_average_scores(test_gt_files, pred_files)
            test.print_scores(scores)
            result_file = os.path.join(
                exp_dir, 'results_fold_{}.yaml'.format(test_fold))
            yaml.dump(dict(scores=scores,
                           chord_train_losses=map(float, crd_train_losses),
                           chord_val_losses=map(float, crd_val_losses),
                           chord_val_accs=map(float, crd_val_accs),
                           chroma_train_losses=map(float, crm_train_losses),
                           chroma_val_losses=map(float, crm_val_losses),
                           chroma_val_accs=map(float, crm_val_accs)),
                      open(result_file, 'w'))
            ex.add_artifact(result_file)

            # close all files
            del train_set
            del val_set
            del test_set
            del gt_files

        # if there is something to aggregate
        if len(datasource['test_fold']) > 1:
            print(Colors.yellow('\nAggregated Results:\n'))
            scores = test.compute_average_scores(all_gt_files, all_pred_files)
            test.print_scores(scores)
            result_file = os.path.join(exp_dir, 'results.yaml')
            yaml.dump(dict(scores=scores), open(result_file, 'w'))
            ex.add_artifact(result_file)

        for pf in all_pred_files:
            ex.add_artifact(pf)

    print(Colors.magenta('Stopping experiment ' + ex.observers[0].hash()))
